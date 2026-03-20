# astrbot-plugin-storage-s3

AstrBot 插件：将用户**引用回复**的文件上传到 S3 兼容对象存储。当前已支持缤纷云 S3，并预留了后续接入七牛云、腾讯云 COS 等云存储的位置。

## 功能说明

- 监听群消息中的 `s3 upload`
- 要求用户先引用一条包含文件、视频、图片或语音的消息，再发送指令
- 插件会下载被引用的文件并上传到配置好的 S3 Bucket
- 上传成功后返回 Bucket、对象 Key 和访问 URL

## 指令用法

1. 先发送一个视频或文件消息
2. 再**引用该消息**发送：

```text
s3 upload
```

如果引用消息中未检测到文件资源，插件会返回失败提示。

## 配置说明

请在插件配置中填写 [`_conf_schema.json`](_conf_schema.json) 定义的字段：

- `provider`：云存储提供商，当前固定为 `s3`
- `s3.endpoint`：缤纷云 S3 的 Endpoint，例如 `https://s3.bitiful.net`
- `s3.region`：区域，例如 `cn-east-1`
- `s3.access_key_id`：Access Key ID
- `s3.secret_access_key`：Secret Access Key
- `s3.bucket`：Bucket 名称
- `s3.public_base_url`：可选，上传成功后用于返回可访问链接前缀
- `s3.key_prefix`：可选，对象前缀，默认 `astrbot/uploads`
- `s3.acl`：可选，上传对象 ACL

## 说明

- 当前实现优先兼容“引用消息里带视频/文件 URL”的场景
- 若不同平台的消息组件字段存在差异，可能需要按具体平台补充提取逻辑
- 后续新增其它云厂商时，可继续扩展 [`main.py`](main.py) 中的 [`BaseStorageProvider`](main.py:20) 与具体 provider 实现
