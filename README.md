# astrbot-plugin-storage-s3

AstrBot 插件：将用户**引用回复**的文件上传到 S3 兼容对象存储，并支持查询对象列表与对象详情。当前已兼容 Amazon S3、缤纷云 S3，并预留了后续接入七牛云、腾讯云 COS 等云存储的位置。

## 功能说明

- 支持指令组 [`/s3 upload`](README.md) 上传引用文件
- 支持指令组 [`/s3 list`](README.md) 获取对象列表
- 支持指令组 [`/s3 detail {fileID}`](README.md) 获取对象详情
- 上传功能要求用户先引用一条包含文件、视频、图片或语音的消息，再发送上传指令
- 兼容旧指令 `s3上传`
- 上传成功后返回 Bucket、对象 Key 和访问 URL

## 指令用法

### 1. 上传引用文件

1. 先发送一个视频或文件消息
2. 再**引用该消息**发送：

```text
/s3 upload
```

也兼容旧指令：

```text
s3上传
```

### 2. 获取文件列表

直接发送：

```text
/s3 list
```

插件会调用 S3 兼容 API 的 `ListObjectsV2`，返回当前 Bucket 下的对象列表。
默认最多返回 20 条，可通过 [`_conf_schema.json`](_conf_schema.json) 中的 `s3.list_max_keys` 与 `s3.list_prefix` 调整默认查询范围。

### 3. 获取文件详情

直接发送：

```text
/s3 detail {fileID}
```

其中 `fileID` 即对象的 Key，可从 [`/s3 list`](README.md) 返回结果中的 `ID` 字段直接复制使用。
插件会调用 S3 兼容 API 的 `HeadObject`，返回大小、类型、ETag、存储类型、修改时间、元数据和访问 URL。

## 配置说明

请在插件配置中填写 [`_conf_schema.json`](_conf_schema.json) 定义的字段：

- `provider`：云存储提供商，当前固定为 `s3`
- `s3.endpoint`：S3 Endpoint，例如 `https://s3.amazonaws.com` 或 `https://s3.bitiful.net`
- `s3.region`：区域，例如 `ap-southeast-1`、`cn-east-1`
- `s3.access_key_id`：Access Key ID
- `s3.secret_access_key`：Secret Access Key
- `s3.bucket`：Bucket 名称
- `s3.public_base_url`：可选，用于拼接返回的访问链接
- `s3.key_prefix`：可选，上传对象前缀，默认 `astrbot/uploads`
- `s3.acl`：可选，上传对象 ACL
- `s3.list_max_keys`：可选，`/s3 list` 默认最大返回数量，范围 1-100
- `s3.list_prefix`：可选，`/s3 list` 默认查询前缀

## 说明

- 文件列表功能基于 S3 标准 `ListObjectsV2`
- 文件详情功能基于 S3 标准 `HeadObject`
- 未配置 `public_base_url` 时，返回的地址格式为 `s3://bucket/key`
- 对于缤纷云的 `https://s3.bitiful.net` 这类 endpoint，插件会自动使用 path-style 寻址，避免 Bucket 名拼接到主机名后触发 SSL 证书域名不匹配问题
- 若不同平台的消息组件字段存在差异，上传场景可能仍需按具体平台补充提取逻辑
- 后续新增其它云厂商时，可继续扩展 [`main.py`](main.py) 中的 [`BaseStorageProvider`](main.py:24) 与具体 provider 实现
