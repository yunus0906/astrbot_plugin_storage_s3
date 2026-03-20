from __future__ import annotations

import asyncio
import mimetypes
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse
from urllib.request import urlretrieve

import boto3
from botocore.config import Config as BotoConfig
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Video
from astrbot.api.star import Context, Star, register


class StorageError(Exception):
    pass


class BaseStorageProvider:
    def upload_file(self, file_path: str, object_key: str, content_type: str | None = None) -> dict[str, Any]:
        raise NotImplementedError

    def list_files(self, max_keys: int = 20, prefix: str | None = None) -> list[dict[str, Any]]:
        raise NotImplementedError

    def get_file_detail(self, file_id: str) -> dict[str, Any]:
        raise NotImplementedError

    def get_download_url(self, file_id: str) -> dict[str, Any]:
        raise NotImplementedError


class S3StorageProvider(BaseStorageProvider):
    def __init__(self, config: dict[str, Any]):
        self._config = config or {}

    def upload_file(self, file_path: str, object_key: str, content_type: str | None = None) -> dict[str, Any]:
        client, bucket, _, acl, public_base_url = self._build_client_context()

        extra_args: dict[str, Any] = {}
        if content_type:
            extra_args["ContentType"] = content_type
        if acl:
            extra_args["ACL"] = acl

        if extra_args:
            client.upload_file(file_path, bucket, object_key, ExtraArgs=extra_args)
        else:
            client.upload_file(file_path, bucket, object_key)

        file_url = self._build_file_url(object_key, public_base_url)
        return {
            "bucket": bucket,
            "key": object_key,
            "url": file_url,
            "provider": "s3",
        }

    def list_files(self, max_keys: int = 20, prefix: str | None = None) -> list[dict[str, Any]]:
        client, bucket, _, _, public_base_url = self._build_client_context()
        normalized_prefix = (prefix or "").strip().strip("/")
        list_kwargs: dict[str, Any] = {
            "Bucket": bucket,
            "MaxKeys": max(1, min(max_keys, 100)),
            "EncodingType": "url",
        }
        if normalized_prefix:
            list_kwargs["Prefix"] = normalized_prefix + "/"

        try:
            response = client.list_objects_v2(**list_kwargs)
        except Exception as exc:
            error_code = ""
            if hasattr(exc, "response"):
                error_code = (exc.response.get("Error") or {}).get("Code", "")
            if error_code in ("NoSuchKey", "NoSuchBucket"):
                return []
            raise StorageError(f"列举文件失败：{exc}") from exc

        contents = response.get("Contents") or []
        return [
            self._normalize_object_summary(bucket, item, public_base_url)
            for item in contents
            if item.get("Key")
        ]

    def get_file_detail(self, file_id: str) -> dict[str, Any]:
        client, bucket, _, _, public_base_url = self._build_client_context()
        object_key = self._normalize_file_id(file_id)
        logger.debug(f"[detail] bucket={bucket} key={object_key}")
        try:
            response = client.head_object(Bucket=bucket, Key=object_key)
        except Exception as exc:
            raise StorageError(f"获取文件详情失败：{exc}") from exc

        return self._normalize_head_object(bucket, object_key, response, public_base_url)

    def get_download_url(self, file_id: str) -> dict[str, Any]:
        client, bucket, _, _, public_base_url = self._build_client_context()
        object_key = self._normalize_file_id(file_id)
        logger.debug(f"[download] bucket={bucket} key={object_key}")

        # 优先使用 public_base_url 拼出直链，稳定可靠，不依赖签名
        if public_base_url:
            download_url = self._build_file_url(object_key, public_base_url)
            return {
                "file_id": object_key,
                "bucket": bucket,
                "key": object_key,
                "download_url": download_url,
                "url": download_url,
                "expires_in": None,
            }

        # 无 public_base_url 时回退预签名 URL
        try:
            presigned_url = client.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket, "Key": object_key},
                ExpiresIn=3600,
            )
        except Exception as exc:
            raise StorageError(f"获取下载链接失败：{exc}") from exc

        return {
            "file_id": object_key,
            "bucket": bucket,
            "key": object_key,
            "download_url": presigned_url,
            "url": self._build_file_url(object_key, public_base_url),
            "expires_in": 3600,
        }

    def _build_client_context(self) -> tuple[Any, str, str, str, str]:
        endpoint = (self._config.get("endpoint") or "").strip()
        region = (self._config.get("region") or "").strip()
        access_key_id = (self._config.get("access_key_id") or "").strip()
        secret_access_key = (self._config.get("secret_access_key") or "").strip()
        bucket = (self._config.get("bucket") or "").strip()
        acl = (self._config.get("acl") or "").strip()
        public_base_url = (self._config.get("public_base_url") or "").strip().rstrip("/")

        missing_fields = [
            name
            for name, value in {
                "endpoint": endpoint,
                "region": region,
                "access_key_id": access_key_id,
                "secret_access_key": secret_access_key,
                "bucket": bucket,
            }.items()
            if not value
        ]
        if missing_fields:
            raise StorageError(f"S3 配置缺失：{', '.join(missing_fields)}")

        normalized_endpoint = self._normalize_endpoint(endpoint, bucket)
        logger.debug(f"S3 endpoint 原始={endpoint} 规范化={normalized_endpoint}")

        client = boto3.client(
            "s3",
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            endpoint_url=normalized_endpoint,
            region_name=region,
            config=BotoConfig(
                signature_version="s3v4",
                s3={"addressing_style": "virtual"},
            ),
        )
        return client, bucket, normalized_endpoint, acl, public_base_url

    def _normalize_endpoint(self, endpoint: str, bucket: str) -> str:
        """
        将含 bucket 子域的 endpoint 还原为根域，避免 boto3 重复拼接 bucket。

        示例：
          https://my-bucket.s3.bitiful.net  ->  https://s3.bitiful.net
          https://my-bucket.s3.amazonaws.com  ->  https://s3.amazonaws.com
          https://s3.bitiful.net  ->  https://s3.bitiful.net  (不变)
        """
        if not endpoint or not bucket:
            return endpoint

        parsed = urlparse(endpoint)
        hostname = parsed.hostname or ""
        bucket_prefix = bucket.lower() + "."

        if hostname.lower().startswith(bucket_prefix):
            new_hostname = hostname[len(bucket_prefix):]
            port = parsed.port
            new_netloc = f"{new_hostname}:{port}" if port else new_hostname
            normalized = parsed._replace(netloc=new_netloc).geturl()
            logger.debug(f"endpoint 含 bucket 子域，已规范化：{endpoint} -> {normalized}")
            return normalized

        return endpoint

    def _build_file_url(self, object_key: str, public_base_url: str) -> str:
        if public_base_url:
            return f"{public_base_url}/{quote(object_key)}"
        return f"s3://{self._config.get('bucket', '').strip()}/{object_key}"

    def _normalize_object_summary(self, bucket: str, item: dict[str, Any], public_base_url: str) -> dict[str, Any]:
        raw_key = str(item.get("Key") or "").strip()
        clean_key = self._strip_bucket_prefix(raw_key, bucket)
        return {
            "file_id": clean_key,
            "bucket": bucket,
            "key": clean_key,
            "raw_key": raw_key,
            "size": int(item.get("Size") or 0),
            "etag": str(item.get("ETag") or "").strip('"'),
            "last_modified": self._format_datetime(item.get("LastModified")),
            "storage_class": str(item.get("StorageClass") or "STANDARD"),
            "url": self._build_file_url(clean_key, public_base_url),
        }

    def _normalize_head_object(
        self,
        bucket: str,
        object_key: str,
        response: dict[str, Any],
        public_base_url: str,
    ) -> dict[str, Any]:
        metadata = response.get("Metadata") or {}
        return {
            "file_id": object_key,
            "bucket": bucket,
            "key": object_key,
            "size": int(response.get("ContentLength") or 0),
            "content_type": str(response.get("ContentType") or "application/octet-stream"),
            "etag": str(response.get("ETag") or "").strip('"'),
            "last_modified": self._format_datetime(response.get("LastModified")),
            "storage_class": str(response.get("StorageClass") or "STANDARD"),
            "metadata": metadata,
            "url": self._build_file_url(object_key, public_base_url),
        }

    def _normalize_file_id(self, file_id: str) -> str:
        normalized = (file_id or "").strip().lstrip("/")
        if not normalized:
            raise StorageError("fileID 不能为空")
        bucket = (self._config.get("bucket") or "").strip().strip("/")
        return self._strip_bucket_prefix(normalized, bucket)

    def _strip_bucket_prefix(self, key: str, bucket: str) -> str:
        if bucket:
            prefix = bucket + "/"
            if key.startswith(prefix):
                stripped = key[len(prefix):]
                if stripped:
                    return stripped
        return key

    def _format_datetime(self, value: Any) -> str:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc).isoformat()
        return ""


def sanitize_filename(name: str) -> str:
    """
    清理文件名，只保留字母、数字、连字符、下划线和点，其余全部删除。

    示例：
      "ScreenRecording_03-05-2026 00-34-44_1.MP4"
      -> "ScreenRecording_03-05-2026_00-34-44_1.MP4"

      "my file (1) [final].mp4"
      -> "myfile1final.mp4"
    """
    stem = Path(name).stem
    suffix = Path(name).suffix  # 保留原始扩展名，如 .MP4

    # 将空格替换为下划线，再删除所有非安全字符
    safe_stem = stem.replace(" ", "_")
    safe_stem = re.sub(r"[^\w\-]", "", safe_stem)  # \w = [a-zA-Z0-9_]

    # 去掉连续的下划线/连字符，保持整洁
    safe_stem = re.sub(r"[_\-]{2,}", "_", safe_stem)
    safe_stem = safe_stem.strip("_-")

    # 扩展名只保留字母和数字
    safe_suffix = re.sub(r"[^a-zA-Z0-9.]", "", suffix)

    if not safe_stem:
        safe_stem = uuid.uuid4().hex

    return f"{safe_stem}{safe_suffix}"


@register("astrbot_plugin_storage_s3", "yunus", "将引用的文件上传到缤纷云 S3 的插件", "1.4.0")
class StorageS3Plugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self.provider_name: str = str(self.config.get("provider", "s3")).strip().lower() or "s3"
        self.s3_config: dict[str, Any] = self.config.get("s3", {}) or {}

    @filter.command_group("s3")
    def s3(self):
        pass

    @s3.command("upload")
    async def s3_upload(self, event: AstrMessageEvent):
        async for result in self._handle_upload_command(event):
            yield result

    @s3.command("list")
    async def s3_list(self, event: AstrMessageEvent):
        async for result in self._handle_list_command(event):
            yield result

    @s3.command("detail")
    async def s3_detail(self, event: AstrMessageEvent):
        """用法：/s3 detail <fileID>，fileID 可含路径斜杠"""
        file_id = self._extract_subcommand_arg(event, "detail")
        async for result in self._handle_detail_command(event, file_id):
            yield result

    @s3.command("download")
    async def s3_download(self, event: AstrMessageEvent):
        """用法：/s3 download <fileID>，fileID 可含路径斜杠"""
        file_id = self._extract_subcommand_arg(event, "download")
        async for result in self._handle_download_command(event, file_id):
            yield result

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        message_str = (event.message_str or "").strip()
        if message_str != "s3上传":
            return

        async for result in self._handle_upload_command(event):
            yield result

    def _extract_subcommand_arg(self, event: AstrMessageEvent, subcommand: str) -> str:
        """
        从原始消息文本里手动提取子命令后面的完整参数，支持参数中含路径斜杠。

        例如消息 "/s3 download astrbot/uploads/xxx.MP4"
        框架解析后 file_id 可能被截断，这里从 event.message_str 原始文本里取完整路径。
        """
        raw = (event.message_str or "").strip()
        pattern = re.compile(
            r"^/?\s*s3\s+" + re.escape(subcommand) + r"\s+(.+)$",
            re.IGNORECASE,
        )
        m = pattern.match(raw)
        if m:
            return m.group(1).strip()
        return ""

    async def _handle_upload_command(self, event: AstrMessageEvent):
        """引用文件，使用指令【s3上传】上传到 S3 兼容存储"""
        yield event.plain_result(f"开始读取文件")
        try:
            provider = self._build_provider()
        except StorageError as exc:
            yield event.plain_result(f"s3 上传 失败：{exc}")
            return

        file_url, file_name = await self._extract_reply_file(event)
        if not file_url or not file_name:
            yield event.plain_result("s3 上传 失败：未检测到引用消息中的文件或视频")
            return

        # 清理文件名，去除空格和特殊字符，避免 URL 编码歧义
        safe_name = sanitize_filename(file_name)
        if safe_name != file_name:
            logger.info(f"文件名已清理：'{file_name}' -> '{safe_name}'")

        yield event.plain_result(f"文件 {file_name} 读取成功，开始上传...")
        temp_file_path = ""
        try:
            temp_file_path = await self._run_blocking(self._download_reply_file, file_url, safe_name)
            object_key = self._build_object_key(safe_name)
            content_type = mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
            result = await self._run_blocking(provider.upload_file, temp_file_path, object_key, content_type)
            yield event.plain_result(
                "s3 上传成功\n"
                f"原始文件名: {file_name}\n"
                f"存储文件名: {safe_name}\n"
                f"Provider: {result.get('provider')}\n"
                f"Bucket: {result.get('bucket')}\n"
                f"fileID: {result.get('key')}\n"
                f"URL: {result.get('url')}"
            )
        except StorageError as exc:
            yield event.plain_result(f"s3 上传 失败：{exc}")
        except Exception as exc:
            logger.exception("s3 上传 处理异常", exc_info=exc)
            yield event.plain_result(f"s3 上传 异常：{exc}")
        finally:
            if temp_file_path:
                try:
                    Path(temp_file_path).unlink(missing_ok=True)
                except OSError:
                    logger.warning(f"清理临时文件失败：{temp_file_path}")

    async def _handle_list_command(self, event: AstrMessageEvent):
        try:
            provider = self._build_provider()
            max_keys = self._get_list_limit()
            prefix = self._get_list_prefix()
            files = await self._run_blocking(provider.list_files, max_keys, prefix)
        except StorageError as exc:
            yield event.plain_result(f"s3 list 失败：{exc}")
            return
        except Exception as exc:
            logger.exception("s3 list 处理异常", exc_info=exc)
            yield event.plain_result(f"s3 list 异常：{exc}")
            return

        if not files:
            prefix_text = prefix or "（根目录）"
            yield event.plain_result(
                f"s3 list 结果为空\n"
                f"Bucket: {self.s3_config.get('bucket', '')}\n"
                f"Prefix: {prefix_text}"
            )
            return

        lines = [
            "s3 list 成功",
            f"Bucket: {self.s3_config.get('bucket', '')}",
            f"数量: {len(files)}",
        ]
        if prefix:
            lines.append(f"Prefix: {prefix}")
        lines.append("文件列表（使用 /s3 detail <ID> 或 /s3 download <ID> 操作）：")
        for index, item in enumerate(files, start=1):
            size_mb = item.get("size", 0) / 1024 / 1024
            lines.extend([
                f"{index}. ID(下载用这个): {item.get('file_id')}",
                f"   Size: {size_mb:.2f} MB",
                f"   LastModified: {item.get('last_modified')}",
                f"   URL: {item.get('url')}",
            ])
        yield event.plain_result("\n".join(lines))

    async def _handle_detail_command(self, event: AstrMessageEvent, file_id: str):
        normalized_file_id = (file_id or "").strip()
        if not normalized_file_id:
            yield event.plain_result("s3 detail 失败：请使用指令 /s3 detail <fileID>")
            return

        logger.info(f"[detail] 收到 file_id='{normalized_file_id}'")
        try:
            provider = self._build_provider()
            detail = await self._run_blocking(provider.get_file_detail, normalized_file_id)
        except StorageError as exc:
            yield event.plain_result(f"s3 detail 失败：{exc}")
            return
        except Exception as exc:
            logger.exception("s3 detail 处理异常", exc_info=exc)
            yield event.plain_result(f"s3 detail 异常：{exc}")
            return

        lines = [
            "s3 detail 成功",
            f"Bucket: {detail.get('bucket')}",
            f"FileID: {detail.get('file_id')}",
            f"fileID: {detail.get('key')}",
            f"Size: {detail.get('size')} bytes",
            f"ContentType: {detail.get('content_type')}",
            f"ETag: {detail.get('etag')}",
            f"StorageClass: {detail.get('storage_class')}",
            f"LastModified: {detail.get('last_modified')}",
            f"URL: {detail.get('url')}",
        ]
        metadata = detail.get("metadata") or {}
        if metadata:
            lines.append("Metadata:")
            for key, value in metadata.items():
                lines.append(f"  {key}: {value}")
        yield event.plain_result("\n".join(lines))

    async def _handle_download_command(self, event: AstrMessageEvent, file_id: str):
        normalized_file_id = (file_id or "").strip()
        if not normalized_file_id:
            yield event.plain_result("s3 download 失败：请使用指令 /s3 download <fileID>")
            return

        logger.info(f"[download] 收到 file_id='{normalized_file_id}'")
        try:
            provider = self._build_provider()
            result = await self._run_blocking(provider.get_download_url, normalized_file_id)
        except StorageError as exc:
            yield event.plain_result(f"s3 download 失败：{exc}")
            return
        except Exception as exc:
            logger.exception("s3 download 处理异常", exc_info=exc)
            yield event.plain_result(f"s3 download 异常：{exc}")
            return

        download_url = result.get("download_url") or ""
        expires_in = result.get("expires_in")

        info_lines = [
            "s3 download 文件信息: ",
            f"Bucket: {result.get('bucket')}",
            f"fileID: {result.get('key')}",
            f"URL: {download_url}",
        ]
        if expires_in:
            info_lines.append(f"有效期: {expires_in} 秒（预签名链接）")
        yield event.plain_result("\n".join(info_lines))

        yield event.plain_result(f"获取链接开始下载视频...")

        if not download_url:
            yield event.plain_result("s3 download 失败：未生成有效的下载链接")
            return

        logger.info(f"[download] 发送视频 URL: {download_url}")
        try:
            yield event.chain_result([Video.fromURL(url=download_url)])
        except Exception as exc:
            logger.warning(f"发送视频消息失败，降级为文本链接：{exc}")
            yield event.plain_result(f"视频发送失败，请手动访问下载链接：\n{download_url}")

    def _build_provider(self) -> BaseStorageProvider:
        if self.provider_name == "s3":
            return S3StorageProvider(self.s3_config)
        raise StorageError(f"暂不支持的云存储提供商：{self.provider_name}")

    def _get_list_limit(self) -> int:
        raw_value = self.s3_config.get("list_max_keys", 20)
        try:
            return max(1, min(int(raw_value), 100))
        except (TypeError, ValueError):
            return 20

    def _get_list_prefix(self) -> str:
        raw = str(self.s3_config.get("list_prefix") or "").strip().strip("/")
        if raw:
            logger.debug(f"s3 list prefix: '{raw}'")
        return raw

    async def _extract_reply_file(self, event: AstrMessageEvent) -> tuple[str | None, str | None]:
        message_components = getattr(event.message_obj, "message", None)
        if not message_components:
            return None, None

        components = message_components if isinstance(message_components, list) else [message_components]
        for component in components:
            reply_components = await self._pick_reply_components(component)
            if reply_components:
                url, name = await self._extract_file_from_components(reply_components)
                if url and name:
                    return url, name
        return None, None

    async def _pick_reply_components(self, component: Any) -> Any:
        for attr in ("message", "chain", "components"):
            value = getattr(component, attr, None)
            if value:
                return value

        nested_data = getattr(component, "data", None)
        if isinstance(nested_data, dict):
            for key in ("message", "chain", "components"):
                value = nested_data.get(key)
                if value:
                    return value

        get_message = getattr(component, "get_message", None)
        if callable(get_message):
            try:
                value = await get_message()
            except Exception as exc:
                logger.debug(f"获取引用消息失败：{exc}")
            else:
                if value:
                    return value

        for attr in ("message", "chain", "components"):
            getter = getattr(component, f"get_{attr}", None)
            if callable(getter):
                try:
                    value = await getter()
                except Exception as exc:
                    logger.debug(f"异步获取 {attr} 失败：{exc}")
                else:
                    if value:
                        return value
        return None

    async def _extract_file_from_components(self, components: Any) -> tuple[str | None, str | None]:
        items = components if isinstance(components, list) else [components]
        for item in items:
            item_type = str(getattr(item, "type", "") or "").lower()
            file_types = {
                "video", "file", "image", "record",
                "componenttype.file", "componenttype.video",
                "componenttype.image", "componenttype.record",
            }

            if item_type in file_types:
                file_url = None
                file_name = None

                get_file = getattr(item, "get_file", None)
                if callable(get_file):
                    try:
                        file_obj = await get_file()
                        if file_obj:
                            file_url = getattr(file_obj, "url", None) or getattr(file_obj, "file_url", None)
                            file_name = getattr(file_obj, "name", None) or getattr(file_obj, "file_name", None)
                    except Exception as exc:
                        logger.debug(f"await get_file() 失败：{exc}")

                if not file_url:
                    file_url = self._pick_first_str(item, "url", "file_url", "src", "path")
                if not file_name:
                    file_name = self._pick_first_str(item, "name", "file_name", "filename")

                if not file_name and file_url:
                    file_name = Path(file_url.split("?")[0]).name
                if file_url and file_name:
                    return file_url, file_name

            nested_data = getattr(item, "data", None)
            if isinstance(nested_data, dict):
                nested_type = str(nested_data.get("type", item_type) or "").lower()
                file_url = self._pick_first_dict_str(nested_data, "file", "file_", "url", "file_url", "src", "path")
                file_name = self._pick_first_dict_str(nested_data, "name", "file_name", "filename")
                if nested_type in file_types and file_url:
                    if not file_name:
                        file_name = Path(file_url.split("?")[0]).name
                    return file_url, file_name

        return None, None

    def _pick_first_str(self, obj: Any, *names: str) -> str | None:
        for name in names:
            value = getattr(obj, name, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def _pick_first_dict_str(self, obj: dict[str, Any], *names: str) -> str | None:
        for name in names:
            value = obj.get(name)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def _build_object_key(self, file_name: str) -> str:
        """构建 object key，file_name 此时已经过 sanitize_filename 清理。"""
        key_prefix = str(self.s3_config.get("key_prefix") or "").strip().strip("/")
        # 不再额外处理文件名，sanitize_filename 已保证安全
        safe_name = Path(file_name).name or f"upload-{uuid.uuid4().hex}"
        unique_name = f"{uuid.uuid4().hex}_{safe_name}"
        return f"{key_prefix}/{unique_name}" if key_prefix else unique_name

    def _download_reply_file(self, file_url: str, file_name: str) -> str:
        suffix = Path(file_name).suffix or ".bin"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_file:
            temp_path = temp_file.name
        urlretrieve(file_url, temp_path)
        return temp_path

    async def _run_blocking(self, func, *args):
        return await asyncio.to_thread(func, *args)

    async def initialize(self):
        logger.info("storage_s3 插件已初始化")

    async def terminate(self):
        logger.info("storage_s3 插件已卸载")