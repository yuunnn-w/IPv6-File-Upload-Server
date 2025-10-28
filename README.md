# IPv6-File-Upload-Server
这是一个基于Python aiohttp的高性能文件上传服务，支持大文件分块上传、实时进度显示、文件完整性验证和WebSocket实时通信。服务支持断点续传、文件覆盖确认、上传速度监控等功能，可处理超大文件，并专门优化支持IPv6网络环境。

## 主要特性
- 大文件分块上传（默认128KB/块）
- 实时上传进度监控（通过WebSocket）
- 文件完整性验证（SHA256哈希校验）
- 断点续传支持
- 文件覆盖确认机制
- 上传速度和剩余时间估算
- 支持IPv6访问
- 前端友好界面

## 安装说明

1. 克隆或下载项目代码
2. 安装依赖包：pip install -r requirements.txt
3. 运行服务：python main.py
4. 访问 http://localhost:8080 开始使用

## 使用方法
- 通过网页界面选择文件进行上传
- 查看实时上传进度和速度
- 支持暂停、继续、取消上传操作
- 上传完成后自动验证文件完整性
- 查看上传目录中的文件列表

## 技术栈
- 后端：Python 3.7+，aiohttp
- 前端：HTML/CSS/JavaScript
- 协议：HTTP/HTTPS，WebSocket
- 安全：SHA256文件完整性校验

## 配置参数
- 上传目录：uploads
- 分块大小：128KB
- 最大文件大小：10GB
- 最大校验线程数：4

