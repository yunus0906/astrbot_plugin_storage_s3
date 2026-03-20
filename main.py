from __future__ import annotations

import asyncio
import mimetypes
import tempfile
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.request import urlretrieve

import boto3
from botocore.config import Config as BotoConfig
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


class StorageError(Exception):
    pass


class BaseStorageProvider:
    def upload_file(self, file_path: str, object_key: str, content_type: str | None = None) -> dict[str, Any]:
        raise NotImplementedError


class S3StorageProvider(BaseStorageProvider):
    def __init__(self, config: dict[str, Any]):
        self._config = config or {}

    def upload_file(self, file_path: str, object_key: str, content_type: str | None = None) -> dict[str, Any]:
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

        client = boto3.client(
            "s3",
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            endpoint_url=endpoint,
            region_name=region,
            config=BotoConfig(signature_version="s3v4"),
        )

        extra_args: dict[str, Any] = {}
        if content_type:
            extra_args["ContentType"] = content_type
        if acl:
            extra_args["ACL"] = acl

        if extra_args:
            client.upload_file(file_path, bucket, object_key, ExtraArgs=extra_args)
        else:
            client.upload_file(file_path, bucket, object_key)

        file_url = f"s3://{bucket}/{object_key}"
        if public_base_url:
            file_url = f"{public_base_url}/{quote(object_key)}"

        return {
            "bucket": bucket,
            "key": object_key,
            "url": file_url,
            "provider": "s3",
        }


@register("astrbot_plugin_storage_s3", "yunus", "将引用的文件上传到缤纷云 S3 的插件", "1.0.0")
class StorageS3Plugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self.provider_name: str = str(self.config.get("provider", "s3")).strip().lower() or "s3"
        self.s3_config: dict[str, Any] = self.config.get("s3", {}) or {}

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        message_str = (event.message_str or "").strip()
        if message_str != "s3 upload":
            return

        try:
            provider = self._build_provider()
        except StorageError as exc:
            yield event.plain_result(f"s3 upload 失败：{exc}")
            return

        file_url, file_name = await self._extract_reply_file(event)
        if not file_url or not file_name:
            yield event.plain_result("s3 upload 失败：未检测到引用消息中的文件或视频")
            return

        yield event.plain_result(f"文件{file_name}读取成功，开始上传..")
        temp_file_path = ""
        try:
            temp_file_path = await self._run_blocking(self._download_reply_file, file_url, file_name)
            object_key = self._build_object_key(file_name)
            content_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
            result = await self._run_blocking(provider.upload_file, temp_file_path, object_key, content_type)
            yield event.plain_result(
                "s3 upload 上传成功\n"
                f"Provider: {result.get('provider')}\n"
                f"Bucket: {result.get('bucket')}\n"
                f"Key: {result.get('key')}\n"
                f"URL: {result.get('url')}\n"
                f"文件名: {file_name}"
            )
        except StorageError as exc:
            yield event.plain_result(f"s3 upload 失败：{exc}")
        except Exception as exc:
            logger.exception("s3 upload 处理异常", exc_info=exc)
            yield event.plain_result(f"s3 upload 异常：{exc}")
        finally:
            if temp_file_path:
                try:
                    Path(temp_file_path).unlink(missing_ok=True)
                except OSError:
                    logger.warning(f"清理临时文件失败：{temp_file_path}")

    def _build_provider(self) -> BaseStorageProvider:
        if self.provider_name == "s3":
            return S3StorageProvider(self.s3_config)
        raise StorageError(f"暂不支持的云存储提供商：{self.provider_name}")

    async def _extract_reply_file(self, event: AstrMessageEvent) -> tuple[str | None, str | None]:
        message_components = getattr(event.message_obj, "message", None)
        if not message_components:
            return None, None

        components = message_components if isinstance(message_components, list) else [message_components]
        for component in components:
            reply_components = await self._pick_reply_components(component)
            if reply_components:
                url, name = self._extract_file_from_components(reply_components)
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

    def _extract_file_from_components(self, components: Any) -> tuple[str | None, str | None]:
        items = components if isinstance(components, list) else [components]
        for item in items:
            item_type = str(getattr(item, "type", "") or "").lower()
            if item_type in {"video", "file", "image", "record", "componenttype.file", "componenttype.video", "componenttype.image", "componenttype.record"}:
                file_url = self._pick_first_str(
                    item,
                    "file",
                    "file_",
                    "url",
                    "file_url",
                    "src",
                    "path",
                )
                file_name = self._pick_first_str(
                    item,
                    "name",
                    "file_name",
                    "filename",
                )
                if not file_name and file_url:
                    file_name = Path(file_url.split("?")[0]).name
                if file_url and file_name:
                    return file_url, file_name

            nested_data = getattr(item, "data", None)
            if isinstance(nested_data, dict):
                file_url = self._pick_first_dict_str(
                    nested_data,
                    "file",
                    "file_",
                    "url",
                    "file_url",
                    "src",
                    "path",
                )
                file_name = self._pick_first_dict_str(
                    nested_data,
                    "name",
                    "file_name",
                    "filename",
                )
                nested_type = str(nested_data.get("type", item_type) or "").lower()
                if nested_type in {"video", "file", "image", "record", "componenttype.file", "componenttype.video", "componenttype.image", "componenttype.record"} and file_url:
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
        key_prefix = str(self.s3_config.get("key_prefix") or "").strip().strip("/")
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
