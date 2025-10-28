import os
import json
import asyncio
import time
import threading
import queue
from pathlib import Path
from aiohttp import web, WSMsgType
import logging

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 配置参数
CONFIG = {
    'upload_dir': "uploads",  # 保存路径
    'chunk_size': 131072,  # 128KB per chunk
    'max_workers': 4,  # 校验线程数
    'max_file_size': 10 * 1024 * 1024 * 1024  # 10GB最大文件大小
}

class FileUploadServer:
    def __init__(self, upload_dir="uploads"):
        self.upload_dir = Path(CONFIG['upload_dir'])
        self.upload_dir.mkdir(exist_ok=True)
        
        # 存储上传状态
        self.upload_status = {}
        self.upload_start_time = {}
        
        # WebSocket连接管理 - 使用线程安全的集合
        self.websockets = set()
        self.ws_lock = threading.Lock()  # 添加锁保护WebSocket集合
        
        # 上传任务管理
        self.upload_tasks = {}
    
    def sha256_hex(self, data):
        """使用Python内置hashlib计算SHA256"""
        import hashlib
        return hashlib.sha256(data).hexdigest()
    
    def get_file_hash(self, filepath):
        """计算文件的SHA256哈希值"""
        import hashlib
        hash_sha256 = hashlib.sha256()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_sha256.update(chunk)
        return hash_sha256.hexdigest()
    
    async def handle_upload_status(self, request):
        """获取上传状态"""
        file_id = request.query.get('file_id')
        if not file_id:
            return web.json_response({'error': 'Missing file_id'}, status=400)
        
        status = self.upload_status.get(file_id, {})
        start_time = self.upload_start_time.get(file_id)
        
        # 计算上传速度
        speed_info = {}
        if start_time and status.get('uploaded_size'):
            elapsed_time = time.time() - start_time
            uploaded_size = status.get('uploaded_size', 0)
            if elapsed_time > 0:
                speed_info['bytes_per_second'] = uploaded_size / elapsed_time
                speed_info['mb_per_second'] = speed_info['bytes_per_second'] / (1024 * 1024)
        
        return web.json_response({
            'file_id': file_id,
            'uploaded_size': status.get('uploaded_size', 0),
            'total_size': status.get('total_size', 0),
            'status': status.get('status', 'unknown'),
            'chunks_received': status.get('chunks_received', 0),
            'chunks_verified': status.get('chunks_verified', 0),
            'expected_file_hash': status.get('expected_file_hash', ''),
            'actual_file_hash': status.get('actual_file_hash', ''),
            'integrity_verified': status.get('integrity_verified', False),
            'speed_info': speed_info
        })
    
    async def handle_chunk_upload(self, request):
        """处理分块上传请求"""
        try:
            reader = await request.multipart()
        
            file_id = None
            filename = None
            file_size = None
            chunk_index = None
            total_chunks = None
            chunk_data = b""
            chunk_hash = None  # 客户端提供的chunk哈希
            expected_file_hash = None  # 客户端提供的完整文件哈希
            allow_overwrite = False  # 默认不允许覆盖
            overwrite_confirmed = False  # 默认未确认覆盖
        
            # 解析multipart数据
            while True:
                part = await reader.next()
                if part is None:
                    break
                
                if part.name == 'file_id':
                    file_id = (await part.read()).decode()
                elif part.name == 'filename':
                    filename = (await part.read()).decode()
                elif part.name == 'file_size':
                    file_size = int((await part.read()).decode())
                elif part.name == 'chunk_index':
                    chunk_index = int((await part.read()).decode())
                elif part.name == 'total_chunks':
                    total_chunks = int((await part.read()).decode())
                elif part.name == 'chunk_data':
                    chunk_data = await part.read()
                elif part.name == 'chunk_hash':
                    chunk_hash = (await part.read()).decode()  # 客户端提供的chunk哈希
                elif part.name == 'expected_file_hash':
                    expected_file_hash = (await part.read()).decode()  # 客户端提供的完整文件哈希
                elif part.name == 'allow_overwrite':
                    allow_overwrite = (await part.read()).decode().lower() == 'true'
                elif part.name == 'overwrite_confirmed':
                    overwrite_confirmed = (await part.read()).decode().lower() == 'true'
        
            # 验证必需参数
            if not all([file_id, filename, chunk_data, chunk_index is not None, total_chunks is not None]):
                return web.json_response({'error': 'Missing required parameters'}, status=400)
        
            # 检查文件大小限制
            if file_size > CONFIG['max_file_size']:
                return web.json_response({'error': 'File too large'}, status=400)
        
            # 构建目标文件路径
            file_path = self.upload_dir / filename

            # 检查文件是否已存在且未确认覆盖
            if file_path.exists() and not overwrite_confirmed:
                # 获取当前文件列表
                files = []
                for file_path_iter in self.upload_dir.iterdir():
                    if file_path_iter.is_file():
                        stat = file_path_iter.stat()
                        files.append({
                            'name': file_path_iter.name,
                            'size': stat.st_size,
                            'size_formatted': self.format_file_size(stat.st_size),
                            'modified': stat.st_mtime
                        })
                files.sort(key=lambda x: x['modified'], reverse=True)
            
                # 文件已存在但未确认覆盖，返回需要确认覆盖的信息
                return web.json_response({
                    'error': 'File exists',
                    'need_overwrite_confirm': True,
                    'filename': filename,
                    'file_size': file_path.stat().st_size,
                    'file_list': files
                }, status=409)

            # ✅ 关键修复：如果确认覆盖且文件存在，则删除旧文件
            if overwrite_confirmed and file_path.exists():
                try:
                    file_path.unlink()
                    logger.info(f"覆盖上传：已删除旧文件 {filename}")
                except Exception as e:
                    logger.error(f"覆盖上传：删除旧文件失败 {filename}: {e}")
                    return web.json_response({'error': 'Failed to delete existing file for overwrite'}, status=500)

            # 初始化或更新上传状态 - 修复：确保状态字典结构完整
            if file_id not in self.upload_status:
                self.upload_status[file_id] = {
                    'filename': filename,
                    'total_size': file_size,
                    'total_chunks': total_chunks,
                    'uploaded_size': 0,
                    'chunks_received': 0,
                    'chunks_verified': 0,
                    'received_chunks': {},
                    'status': 'uploading',
                    'start_time': time.time(),
                    'chunk_hashes': [None] * total_chunks,
                    'error_chunks': [],
                    'verified_chunks': set(),
                    'expected_file_hash': expected_file_hash,  # 客户端提供的文件哈希
                    'actual_file_hash': '',  # 实际组装后文件的哈希
                    'integrity_verified': False  # 完整性验证状态
                }
                self.upload_start_time[file_id] = time.time()
            else:
                # 如果是覆盖上传或重新上传，重置进度相关数据
                if overwrite_confirmed or self.upload_status[file_id].get('status') in ['cancelled', 'completed', 'failed']:
                    self.upload_status[file_id] = {
                        'filename': filename,
                        'total_size': file_size,
                        'total_chunks': total_chunks,
                        'uploaded_size': 0,
                        'chunks_received': 0,
                        'chunks_verified': 0,
                        'received_chunks': {},  # 重置chunks
                        'status': 'uploading',
                        'start_time': time.time(),
                        'chunk_hashes': [None] * total_chunks,
                        'error_chunks': [],
                        'verified_chunks': set(),
                        'expected_file_hash': expected_file_hash,
                        'actual_file_hash': '',
                        'integrity_verified': False
                    }
                    self.upload_start_time[file_id] = time.time()

            # 验证chunk哈希
            actual_chunk_hash = self.sha256_hex(chunk_data)
            if chunk_hash and actual_chunk_hash != chunk_hash:
                error_msg = f"Chunk {chunk_index} hash mismatch. Expected: {chunk_hash}, Actual: {actual_chunk_hash}"
                logger.error(error_msg)
                return web.json_response({'error': error_msg}, status=400)

            # 保存接收到的 chunk
            upload_info = self.upload_status[file_id]
            upload_info['received_chunks'][chunk_index] = {
                'data': chunk_data,
                'received_time': time.time(),
                'verified': True,  # 修复：上传时立即标记为已验证
                'hash': actual_chunk_hash  # 存储chunk的哈希值
            }
            upload_info['chunks_received'] += 1
            upload_info['chunks_verified'] += 1  # 修复：上传时立即增加验证块数
            upload_info['uploaded_size'] += len(chunk_data)
        
            # 计算进度与速度
            progress = (upload_info['chunks_received'] / total_chunks) * 100
            elapsed_time = time.time() - upload_info['start_time']
            avg_speed = upload_info['uploaded_size'] / elapsed_time if elapsed_time > 0 else 0
            avg_speed_mb = avg_speed / (1024 * 1024)
        
            estimated_remaining_time = None
            if avg_speed > 0:
                remaining_bytes = upload_info['total_size'] - upload_info['uploaded_size']
                estimated_remaining_time = remaining_bytes / avg_speed
        
            # 广播进度更新
            progress_data = {
                'type': 'upload_progress',
                'file_id': file_id,
                'filename': filename,
                'progress': progress,
                'uploaded_size': upload_info['uploaded_size'],
                'total_size': file_size,
                'chunks_received': upload_info['chunks_received'],
                'chunks_verified': upload_info['chunks_verified'],
                'average_speed_mb': avg_speed_mb,
                'estimated_remaining_time': estimated_remaining_time
            }
            await self.broadcast_to_websockets(progress_data)
        
            # 检查是否所有 chunk 都已接收，触发组装
            if upload_info['chunks_received'] == total_chunks:
                await self.verify_and_assemble_file(file_id)
        
            return web.json_response({
                'status': 'success',
                'message': 'Chunk received successfully',
                'chunk_index': chunk_index,
                'chunks_received': upload_info['chunks_received'],
                'total_chunks': total_chunks,
                'chunk_hash': actual_chunk_hash,  # 返回chunk的哈希值
                'integrity_verified': upload_info['integrity_verified']
            })
        
        except Exception as e:
            logger.error(f"Chunk upload error: {str(e)}")
            return web.json_response({'error': str(e)}, status=500)
    
    async def verify_and_assemble_file(self, file_id):
        """验证并组装文件"""
        upload_info = self.upload_status.get(file_id)
        if not upload_info:
            logger.error(f"Upload info not found for file_id: {file_id}")
            return
        
        filename = upload_info['filename']
        
        # 创建一个临时文件来组装
        temp_path = self.upload_dir / f"{filename}.tmp.{file_id}"
        
        try:
            with open(temp_path, 'wb') as f:
                for i in range(upload_info['total_chunks']):
                    if i in upload_info['received_chunks']:
                        chunk_data = upload_info['received_chunks'][i]['data']
                        f.write(chunk_data)
                    else:
                        raise Exception(f"Missing chunk {i}")
            
            # 计算完整文件的哈希值
            actual_file_hash = self.get_file_hash(temp_path)
            upload_info['actual_file_hash'] = actual_file_hash
            
            # 验证文件完整性
            expected_file_hash = upload_info.get('expected_file_hash')
            integrity_verified = False
            if expected_file_hash and actual_file_hash == expected_file_hash:
                integrity_verified = True
                logger.info(f"File integrity verified for {filename}. Hash: {actual_file_hash}")
            else:
                if expected_file_hash:
                    logger.error(f"File integrity verification failed for {filename}. Expected: {expected_file_hash}, Actual: {actual_file_hash}")
                else:
                    logger.warning(f"No expected hash provided for {filename}, skipping integrity check")
            
            upload_info['integrity_verified'] = integrity_verified
            
            # 重命名临时文件为正式文件
            final_path = self.upload_dir / filename
            # 如果文件已存在，先删除
            if final_path.exists():
                final_path.unlink()
            temp_path.rename(final_path)
            
            # 更新状态为完成
            upload_info['status'] = 'completed'
            
            # 计算总上传时间
            total_time = time.time() - upload_info['start_time']
            overall_speed = upload_info['total_size'] / total_time if total_time > 0 else 0
            overall_speed_mb = overall_speed / (1024 * 1024)
            
            logger.info(
                f"Upload completed for {filename}. "
                f"Total time: {total_time:.2f}s, "
                f"Overall speed: {overall_speed_mb:.2f} MB/s, "
                f"Integrity verified: {integrity_verified}"
            )
            
            # 广播完成消息
            completion_data = {
                'type': 'upload_complete',
                'file_id': file_id,
                'filename': filename,
                'file_hash': actual_file_hash,
                'expected_file_hash': expected_file_hash,
                'integrity_verified': integrity_verified,
                'upload_time': total_time,
                'average_speed_mb': overall_speed_mb,
                'total_size': upload_info['total_size'],
                'chunk_size': CONFIG['chunk_size'],
                'total_chunks': upload_info['total_chunks'],
                'error_chunks_count': len(upload_info['error_chunks']),
                'chunks_verified': upload_info['chunks_verified']  # 添加已验证块数
            }
            await self.broadcast_to_websockets(completion_data)
            
            # 广播文件列表更新
            await self.broadcast_file_list()
            
            # 清理上传状态（保留完成状态以供查询）
            # upload_info 保留在完成状态，但可以清理一些临时数据
            upload_info['received_chunks'] = {}
            
        except Exception as e:
            logger.error(f"File assembly error: {str(e)}")
            # 删除临时文件
            if temp_path.exists():
                temp_path.unlink()
            
            upload_info['status'] = 'failed'
            error_data = {
                'type': 'upload_error',
                'file_id': file_id,
                'filename': filename,
                'error': str(e)
            }
            await self.broadcast_to_websockets(error_data)
    
    async def handle_resumable_upload(self, request):
        """处理可恢复上传请求"""
        try:
            reader = await request.multipart()
            
            file_id = None
            filename = None
            start_chunk = 0  # 默认从第0个chunk开始
            
            # 解析multipart数据
            while True:
                part = await reader.next()
                if part is None:
                    break
                    
                if part.name == 'file_id':
                    file_id = (await part.read()).decode()
                elif part.name == 'filename':
                    filename = (await part.read()).decode()
                elif part.name == 'start_chunk':
                    start_chunk = int((await part.read()).decode())
            
            if not file_id or not filename:
                return web.json_response({'error': 'Missing file_id or filename'}, status=400)
            
            # 检查文件是否存在
            file_path = self.upload_dir / filename
            if not file_path.exists():
                return web.json_response({'error': 'File not found'}, status=404)
            
            # 读取文件并按chunk发送
            file_size = file_path.stat().st_size
            total_chunks = (file_size + CONFIG['chunk_size'] - 1) // CONFIG['chunk_size']
            
            # 发送从指定chunk开始的数据
            chunks_to_send = []
            with open(file_path, 'rb') as f:
                for i in range(start_chunk, total_chunks):
                    f.seek(i * CONFIG['chunk_size'])
                    chunk_data = f.read(CONFIG['chunk_size'])
                    if not chunk_data:
                        break
                    
                    # 计算chunk的SHA256哈希
                    chunk_hash = self.sha256_hex(chunk_data)
                    
                    chunks_to_send.append({
                        'index': i,
                        'data': chunk_data,
                        'hash': chunk_hash
                    })
            
            return web.json_response({
                'status': 'success',
                'chunks': len(chunks_to_send),
                'data': [c['data'].hex() for c in chunks_to_send],  # 转换为hex字符串
                'hashes': [c['hash'] for c in chunks_to_send]
            })
            
        except Exception as e:
            logger.error(f"Resumable upload error: {str(e)}")
            return web.json_response({'error': str(e)}, status=500)
    
    async def handle_cancel_upload(self, request):
        """处理取消上传请求"""
        try:
            file_id = request.query.get('file_id')
            if not file_id:
                return web.json_response({'error': 'Missing file_id'}, status=400)
            
            # 更新状态为已取消
            if file_id in self.upload_status:
                self.upload_status[file_id]['status'] = 'cancelled'
                # 清理相关数据，但保留状态记录
                if 'received_chunks' in self.upload_status[file_id]:
                    self.upload_status[file_id]['received_chunks'] = {}  # 清空chunks，而不是删除键
                    self.upload_status[file_id]['chunks_received'] = 0
                    self.upload_status[file_id]['chunks_verified'] = 0
                    self.upload_status[file_id]['uploaded_size'] = 0
            
            # 广播取消消息
            cancel_data = {
                'type': 'upload_cancelled',
                'file_id': file_id
            }
            await self.broadcast_to_websockets(cancel_data)
            
            return web.json_response({
                'status': 'success',
                'message': 'Upload cancelled'
            })
            
        except Exception as e:
            logger.error(f"Cancel upload error: {str(e)}")
            return web.json_response({'error': str(e)}, status=500)
    
    async def handle_verify_integrity(self, request):
        """验证文件完整性"""
        try:
            file_id = request.query.get('file_id')
            expected_hash = request.query.get('expected_hash')
            
            if not file_id or not expected_hash:
                return web.json_response({'error': 'Missing file_id or expected_hash'}, status=400)
            
            upload_info = self.upload_status.get(file_id)
            if not upload_info:
                return web.json_response({'error': 'Upload info not found'}, status=404)
            
            filename = upload_info['filename']
            file_path = self.upload_dir / filename
            
            if not file_path.exists():
                return web.json_response({'error': 'File not found'}, status=404)
            
            # 计算实际文件哈希
            actual_hash = self.get_file_hash(file_path)
            integrity_verified = actual_hash == expected_hash
            
            # 更新状态
            upload_info['actual_file_hash'] = actual_hash
            upload_info['integrity_verified'] = integrity_verified
            
            return web.json_response({
                'status': 'success',
                'file_id': file_id,
                'filename': filename,
                'expected_hash': expected_hash,
                'actual_hash': actual_hash,
                'integrity_verified': integrity_verified
            })
            
        except Exception as e:
            logger.error(f"Integrity verification error: {str(e)}")
            return web.json_response({'error': str(e)}, status=500)
    
    async def handle_list_files(self, request):
        """列出上传目录中的文件"""
        try:
            files = []
            for file_path in self.upload_dir.iterdir():
                if file_path.is_file():
                    stat = file_path.stat()
                    files.append({
                        'name': file_path.name,
                        'size': stat.st_size,
                        'size_formatted': self.format_file_size(stat.st_size),
                        'modified': stat.st_mtime,
                        'hash': self.get_file_hash(file_path)  # 添加文件哈希值
                    })
            # 按修改时间排序，最新的在前
            files.sort(key=lambda x: x['modified'], reverse=True)
            return web.json_response({'files': files})
        except Exception as e:
            logger.error(f"List files error: {str(e)}")
            return web.json_response({'error': str(e)}, status=500)
    
    async def handle_download(self, request):
        """处理文件下载请求"""
        try:
            filename = request.query.get('filename')
            if not filename:
                return web.json_response({'error': 'Missing filename'}, status=400)
            
            file_path = self.upload_dir / filename
            if not file_path.exists():
                return web.json_response({'error': 'File not found'}, status=404)
            
            # 返回文件内容
            return web.FileResponse(file_path)
        except Exception as e:
            logger.error(f"Download error: {str(e)}")
            return web.json_response({'error': str(e)}, status=500)
    
    def format_file_size(self, size_bytes):
        """格式化文件大小显示"""
        if size_bytes == 0:
            return "0B"
        size_names = ["B", "KB", "MB", "GB", "TB"]
        import math
        i = int(math.floor(math.log(size_bytes, 1024)))
        p = math.pow(1024, i)
        s = round(size_bytes / p, 2)
        return f"{s} {size_names[i]}"
    
    async def broadcast_file_list(self):
        """广播文件列表更新"""
        try:
            files = []
            for file_path in self.upload_dir.iterdir():
                if file_path.is_file():
                    stat = file_path.stat()
                    files.append({
                        'name': file_path.name,
                        'size': stat.st_size,
                        'size_formatted': self.format_file_size(stat.st_size),
                        'modified': stat.st_mtime,
                        'hash': self.get_file_hash(file_path)  # 添加文件哈希值
                    })
            # 按修改时间排序，最新的在前
            files.sort(key=lambda x: x['modified'], reverse=True)
            
            file_list_data = {
                'type': 'file_list_update',
                'files': files
            }
            await self.broadcast_to_websockets(file_list_data)
        except Exception as e:
            logger.error(f"Broadcast file list error: {str(e)}")
    
    async def broadcast_to_websockets(self, data):
        """广播消息到所有WebSocket连接"""
        with self.ws_lock:  # 使用锁保护WebSocket集合的访问
            if self.websockets:
                disconnected = set()
                for ws in self.websockets.copy():
                    try:
                        await ws.send_str(json.dumps(data))
                    except Exception as e:
                        logger.error(f"Broadcast error: {str(e)}")
                        disconnected.add(ws)
                
                # 移除断开的连接
                for ws in disconnected:
                    self.websockets.discard(ws)
    
    async def handle_websocket(self, request):
        """处理WebSocket连接"""
        # 创建WebSocket响应，设置无限制消息大小
        ws = web.WebSocketResponse(
            max_msg_size=0,  # 0表示无限制
            timeout=300,  # 5分钟超时
            autoping=True
        )
        await ws.prepare(request)
        
        # 添加到WebSocket连接池 - 使用锁保护
        with self.ws_lock:
            self.websockets.add(ws)
        logger.info(f"WebSocket连接建立，当前连接数: {len(self.websockets)}")
        
        try:
            # 连接建立后立即发送文件列表
            await self.broadcast_file_list()
            
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    # 处理客户端发送的消息
                    try: 
                        data = json.loads(msg.data)
                        msg_type = data.get('type')
                        
                        if msg_type == 'ping':
                            # 响应ping消息
                            await ws.send_str(json.dumps({'type': 'pong'}))
                        elif msg_type == 'get_status':
                            # 返回服务器状态
                            with self.ws_lock:
                                connected_clients = len(self.websockets)
                            active_uploads = len([s for s in self.upload_status.values() if s.get('status') == 'uploading'])
                            total_files = len([f for f in self.upload_dir.iterdir() if f.is_file()])
                            
                            status_data = {
                                'type': 'server_status',
                                'connected_clients': connected_clients,
                                'active_uploads': active_uploads,
                                'total_files': total_files
                            }
                            await ws.send_str(json.dumps(status_data))
                        elif msg_type == 'request_resend':
                            # 请求重传指定chunk
                            file_id = data.get('file_id')
                            chunk_indices = data.get('chunk_indices', [])
                            
                            if file_id and chunk_indices:
                                # 从已接收的chunks中获取指定的chunk数据
                                upload_info = self.upload_status.get(file_id)
                                if upload_info and 'received_chunks' in upload_info:
                                    resend_data = []
                                    for idx in chunk_indices:
                                        if idx in upload_info['received_chunks']:
                                            chunk_data = upload_info['received_chunks'][idx]['data']
                                            chunk_hash = self.sha256_hex(chunk_data)
                                            resend_data.append({
                                                'index': idx,
                                                'data': chunk_data.hex(),  # 转换为hex字符串
                                                'hash': chunk_hash
                                            })
                                    
                                    response = {
                                        'type': 'resend_response',
                                        'file_id': file_id,
                                        'chunks': resend_data
                                    }
                                    await ws.send_str(json.dumps(response))
                        elif msg_type == 'download_file':
                            # 处理下载请求
                            filename = data.get('filename')
                            if filename:
                                # 发送下载响应
                                download_response = {
                                    'type': 'download_response',
                                    'filename': filename,
                                    'download_url': f'/download?filename={filename}'
                                }
                                await ws.send_str(json.dumps(download_response))
                    except json.JSONDecodeError:
                        logger.error("Invalid JSON received from WebSocket")
                    except Exception as e:
                        logger.error(f"Error processing WebSocket message: {str(e)}")
                elif msg.type == WSMsgType.ERROR:
                    logger.error(f"WebSocket连接错误: {ws.exception()}")
                    break
        finally:
            # 从连接池中移除 - 使用锁保护
            with self.ws_lock:
                self.websockets.discard(ws)
            logger.info(f"WebSocket连接断开，当前连接数: {len(self.websockets)}")
        
        return ws
    
    async def handle_index(self, request):
        """返回前端上传页面"""
        html_content = """
<!DOCTYPE html>
<html>
<head>
    <title>文件上传</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
            background-color: #f5f5f5;
        }
        .container {
            background: white;
            border-radius: 10px;
            padding: 30px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }
        h1 {
            text-align: center;
            color: #333;
            margin-bottom: 10px;
        }
        .subtitle {
            text-align: center;
            color: #666;
            margin-bottom: 30px;
        }
        .header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
        }
        .personal-info {
            text-align: right;
            font-size: 14px;
            color: #666;
        }
        .personal-info a {
            color: #007bff;
            text-decoration: none;
        }
        .personal-info a:hover {
            text-decoration: underline;
        }
        .connection-status {
            background: #d1ecf1;
            padding: 10px;
            border-radius: 8px;
            margin: 20px 0;
            text-align: center;
            font-weight: bold;
        }
        .connected {
            background: #d4edda;
            color: #155724;
        }
        .disconnected {
            background: #f8d7da;
            color: #721c24;
        }
        .upload-area {
            border: 2px dashed #ccc;
            border-radius: 10px;
            padding: 40px;
            text-align: center;
            margin: 20px 0;
            background-color: #fafafa;
            transition: border-color 0.3s;
        }
        .upload-area:hover {
            border-color: #007bff;
        }
        .upload-area.dragover {
            border-color: #28a745;
            background-color: #f0fff4;
        }
        .controls {
            text-align: center;
            margin: 20px 0;
        }
        .progress-container {
            margin: 20px 0;
        }
        .progress-bar {
            width: 100%;
            height: 24px;
            background-color: #e9ecef;
            border-radius: 12px;
            overflow: hidden;
            position: relative;
            margin: 10px 0;
        }
        .progress-fill {
            height: 100%;
            background: linear-gradient(90deg, #007bff, #0056b3);
            width: 0%;
            transition: width 0.3s;
        }
        .progress-text {
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            display: flex;
            align-items: center;
            justify-content: center;
            color: white;
            font-weight: bold;
            text-shadow: 1px 1px 2px rgba(0,0,0,0.5);
        }
        .speed-info {
            display: flex;
            justify-content: space-between;
            margin: 10px 0;
            font-size: 14px;
            color: #555;
        }
        button {
            background-color: #007bff;
            color: white;
            border: none;
            padding: 10px 20px;
            border-radius: 5px;
            cursor: pointer;
            margin: 5px;
            font-size: 14px;
        }
        button:hover {
            background-color: #0056b3;
        }
        button:disabled {
            background-color: #cccccc;
            cursor: not-allowed;
        }
        .cancel-btn {
            background-color: #dc3545;
        }
        .cancel-btn:hover {
            background-color: #c82333;
        }
        .status {
            margin: 10px 0;
            padding: 10px;
            border-radius: 5px;
            text-align: center;
        }
        .status.success {
            background-color: #d4edda;
            color: #155724;
        }
        .status.error {
            background-color: #f8d7da;
            color: #721c24;
        }
        .status.info {
            background-color: #d1ecf1;
            color: #0c5460;
        }
        .status.completed {
            background-color: #cce7ff;
            color: #004085;
            border: 1px solid #99ccff;
        }
        .file-info {
            margin: 10px 0;
            padding: 10px;
            background-color: #e9ecef;
            border-radius: 5px;
            font-size: 14px;
        }
        input[type="file"] {
            margin: 10px 0;
        }
        .footer {
            text-align: center;
            margin-top: 30px;
            color: #666;
            font-size: 12px;
        }
        .server-info {
            background: #e9f7ef;
            padding: 10px;
            border-radius: 5px;
            margin: 10px 0;
            font-size: 14px;
        }
        .chunk-info {
            font-size: 12px;
            color: #666;
            margin-top: 5px;
        }
        .notification {
            position: fixed;
            bottom: 20px;
            right: 20px;
            background-color: #f8d7da;
            color: #721c24;
            padding: 15px;
            border-radius: 8px;
            border: 1px solid #f5c6cb;
            z-index: 1000;
            box-shadow: 0 4px 12px rgba(0,0,0,0.15);
            max-width: 300px;
        }
        .notification.success {
            background-color: #d4edda;
            color: #155724;
            border-color: #c3e6cb;
        }
        .file-list-container {
            margin: 30px 0;
        }
        .file-list-title {
            font-size: 18px;
            margin-bottom: 10px;
            color: #333;
            border-bottom: 2px solid #007bff;
            padding-bottom: 5px;
        }
        .file-list {
            border-collapse: collapse;
            width: 100%;
        }
        .file-list th, .file-list td {
            border: 1px solid #ddd;
            padding: 12px;
            text-align: left;
        }
        .file-list th {
            background-color: #f2f2f2;
            font-weight: bold;
        }
        .file-list tr:nth-child(even) {
            background-color: #f9f9f9;
        }
        .file-list tr:hover {
            background-color: #f5f5f5;
        }
        .upload-stats {
            background-color: #f0f8ff;
            border: 1px solid #cce7ff;
            border-radius: 5px;
            padding: 15px;
            margin: 15px 0;
            font-size: 14px;
        }
        .upload-stats h4 {
            margin: 0 0 10px 0;
            color: #004085;
            border-bottom: 1px solid #cce7ff;
            padding-bottom: 5px;
        }
        .stats-row {
            display: flex;
            justify-content: space-between;
            margin: 5px 0;
        }
        .stats-label {
            font-weight: bold;
            color: #004085;
        }
        .stats-value {
            color: #004085;
        }
        .upload-progress-info {
            background-color: #e9f7ef;
            border: 1px solid #c3e6cb;
            border-radius: 5px;
            padding: 10px;
            margin: 10px 0;
            font-size: 14px;
        }
        .upload-progress-info .stats-row {
            margin: 3px 0;
        }
        .integrity-status {
            margin: 10px 0;
            padding: 10px;
            border-radius: 5px;
            text-align: center;
            font-weight: bold;
        }
        .integrity-verified {
            background-color: #d4edda;
            color: #155724;
        }
        .integrity-failed {
            background-color: #f8d7da;
            color: #721c24;
        }
        .integrity-pending {
            background-color: #fff3cd;
            color: #856404;
        }
        .hash-cell {
            position: relative;
            max-width: 200px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        .hash-cell:hover {
            overflow: visible;
            white-space: normal;
            word-break: break-all;
        }
        .hash-cell:hover::after {
            content: "";
            position: absolute;
            bottom: 0;
            left: 0;
            right: 0;
            height: 100%;
            background: linear-gradient(transparent, white);
            pointer-events: none;
        }
        /* 添加到现有CSS中的新样式 */
        .file-name-cell {
            position: relative;
            max-width: 200px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }

        .file-name-cell:hover {
            overflow: visible;
            white-space: normal;
            word-break: break-all;
        }

        .file-name-cell:hover::after {
            content: "";
            position: absolute;
            bottom: 0;
            left: 0;
            right: 0;
            height: 100%;
            background: linear-gradient(transparent, white);
            pointer-events: none;
        }
        .download-btn {
            background-color: #28a745;
            color: white;
            border: none;
            padding: 5px 10px;
            border-radius: 3px;
            cursor: pointer;
            font-size: 12px;
            margin-left: 5px;
        }
        .download-btn:hover {
            background-color: #218838;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>IPV6公网网盘</h1>
            <div class="personal-info">
                <p>开发者：小阳</p>
                <p>GitHub项目地址: <a href="https://github.com/yuunnn-w/IPv6-File-Upload-Server" target="_blank">GitHub</a></p>
            </div>
        </div>
        <p class="subtitle">支持大文件分块上传和实时进度显示</p>
	<p class="subtitle">- 注意，文件上传有校验，但文件下载无校验，如需校验请自行计算sha256 -</p>
        
        <div id="connectionStatus" class="connection-status disconnected">
            WebSocket: 连接中...
        </div>
        
        <div class="server-info" id="serverInfo">
            服务器信息: 等待连接...
        </div>
        
        <div class="upload-area" id="uploadArea">
            <input type="file" id="fileInput" style="display: none;">
            <button onclick="document.getElementById('fileInput').click()">选择文件</button>
            <p id="fileName">未选择文件</p>
        </div>
        
        <div class="file-info" id="fileInfo" style="display: none;">
            <strong>文件信息:</strong> <span id="fileDetails"></span>
            <div class="chunk-info">分块大小: 128KB | 预计分块数: <span id="expectedChunks">0</span></div>
        </div>
        
        <div class="controls">
            <button id="uploadBtn" onclick="startUpload()" disabled>开始上传</button>
            <button id="pauseBtn" onclick="pauseUpload()" disabled>暂停上传</button>
            <button id="resumeBtn" onclick="resumeUpload()" disabled>继续上传</button>
            <button id="cancelBtn" class="cancel-btn" onclick="cancelUpload()" disabled>取消上传</button>
        </div>
        
        <div class="progress-container">
            <div class="progress-bar">
                <div class="progress-fill" id="progressFill"></div>
                <div class="progress-text" id="progressText">0%</div>
            </div>
            <div class="speed-info">
                <span>上传速度: <span id="uploadSpeed">0.00</span> MB/s</span>
                <span>剩余时间: <span id="remainingTime">--:--:--</span></span>
            </div>
            <div class="speed-info">
                <span>已接收: <span id="chunksReceived">0</span>/<span id="totalChunks">0</span> 块</span>
                <span>已校验: <span id="chunksVerified">0</span> 块</span>
            </div>
        </div>
        
        <div id="uploadProgressInfo" class="upload-progress-info" style="display: none;">
            <div class="stats-row">
                <span class="stats-label">上传进度:</span>
                <span class="stats-value" id="currentProgressText">选择文件开始上传</span>
            </div>
        </div>
        
        <div id="statusDiv"></div>
        
        <div class="file-list-container">
            <h3 class="file-list-title">上传目录文件列表</h3>
            <table class="file-list" id="fileListTable">
                <thead>
                    <tr>
                        <th>文件名</th>
                        <th>大小</th>
                        <th>修改时间</th>
                        <th>哈希值</th>
                        <th>操作</th>
                    </tr>
                </thead>
                <tbody id="fileListBody">
                    <tr><td colspan="5">正在加载...</td></tr>
                </tbody>
            </table>
        </div>
        
        <div class="footer">
            服务器运行中... | 端口: 8080 | 支持IPv6访问
        </div>
    </div>

    <script src="https://cdnjs.cloudflare.com/ajax/libs/js-sha256/0.9.0/sha256.min.js"></script>
    <script>
        let selectedFile = null;
        let websocket = null;
        let isConnected = false;
        let uploadController = null;  // 用于控制上传
        let isPaused = false;
        let isCancelled = false;
        let currentChunkIndex = 0;
        let totalChunks = 0;
        let uploadedChunks = new Set();  // 记录已成功上传的chunks
        let chunkHashes = {};  // 存储每个chunk的哈希值
        let pendingUpload = null;  // 存储待上传的文件信息，用于覆盖确认后继续上传
        let uploadCompleted = false; // 新增全局标志，防止状态被清空
        let fileHash = null; // 存储文件的哈希值

        // 初始化WebSocket连接
        function initWebSocket() {
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            const wsUrl = `${protocol}//${window.location.host}/ws`;
            
            try {
                websocket = new WebSocket(wsUrl);
                
                websocket.onopen = function(event) {
                    isConnected = true;
                    document.getElementById('connectionStatus').className = 'connection-status connected';
                    document.getElementById('connectionStatus').textContent = 'WebSocket: 已连接';
                    
                    // 发送状态请求
                    websocket.send(JSON.stringify({type: 'get_status'}));
                    
                    // 开始心跳检测
                    startHeartbeat();
                };
                
                websocket.onmessage = function(event) {
                    try {
                        const data = JSON.parse(event.data);
                        console.log('收到 WebSocket 消息，data内容:', data); // 👈 关键调试语句
                        
                        if (data.type === 'upload_progress') {
                            // 更新进度条
                            document.getElementById('progressFill').style.width = data.progress + '%';
                            document.getElementById('progressText').textContent = data.progress.toFixed(1) + '%';
                            
                            // 更新速度信息
                            document.getElementById('uploadSpeed').textContent = data.average_speed_mb.toFixed(2);
                            
                            // 更新剩余时间
                            if (data.estimated_remaining_time !== null) {
                                const remaining = formatTime(data.estimated_remaining_time);
                                document.getElementById('remainingTime').textContent = remaining;
                            } else {
                                document.getElementById('remainingTime').textContent = '--:--:--';
                            }
                            
                            // 更新chunk信息
                            document.getElementById('chunksReceived').textContent = data.chunks_received;
                            document.getElementById('totalChunks').textContent = data.total_chunks || totalChunks;
                            document.getElementById('chunksVerified').textContent = data.chunks_verified || 0;
                            
                            // 更新进度信息显示
                            document.getElementById('currentProgressText').textContent = 
                                `${data.filename} - ${formatFileSize(data.uploaded_size)}/${formatFileSize(data.total_size)}`;
                            document.getElementById('uploadProgressInfo').style.display = 'block';
                            
                            // 清除之前的状态信息，但不重置完成状态
                            if (!uploadCompleted) {
                                document.getElementById('statusDiv').textContent = '';
                                document.getElementById('statusDiv').className = '';
                            }
                        } else if (data.type === 'upload_complete') {
                            // 设置上传完成标志
                            uploadCompleted = true;
                                                        
                            // 在statusDiv中显示详细的上传完成统计信息
                            const filename = data.filename || '未知文件';
                            const total_size = data.total_size || 0;
                            const upload_time = data.upload_time || 0;
                            const average_speed_mb = data.average_speed_mb || 0;
                            const total_chunks = data.total_chunks || 0;
                            const error_chunks_count = data.error_chunks_count || 0;
                            const chunks_verified = data.chunks_verified || 0;
                            const integrity_verified = data.integrity_verified || false;

                            const fullStatsHtml = `
                                <div class="upload-stats">
                                    <h4>上传完成统计信息</h4>
                                    <div class="stats-row">
                                        <span class="stats-label">文件名:</span>
                                        <span class="stats-value">${escapeHtml(filename)}</span>
                                    </div>
                                    <div class="stats-row">
                                        <span class="stats-label">上传总耗时:</span>
                                        <span class="stats-value">${upload_time.toFixed(2)}秒</span>
                                    </div>
                                    <div class="stats-row">
                                        <span class="stats-label">上传总大小:</span>
                                        <span class="stats-value">${formatFileSize(total_size)}</span>
                                    </div>
                                    <div class="stats-row">
                                        <span class="stats-label">分块大小:</span>
                                        <span class="stats-value">${formatFileSize(131072)}</span>
                                    </div>
                                    <div class="stats-row">
                                        <span class="stats-label">分块数量:</span>
                                        <span class="stats-value">${total_chunks}块</span>
                                    </div>
                                    <div class="stats-row">
                                        <span class="stats-label">平均速度:</span>
                                        <span class="stats-value">${average_speed_mb.toFixed(2)} MB/s</span>
                                    </div>
                                    <div class="stats-row">
                                        <span class="stats-label">出错块数:</span>
                                        <span class="stats-value">${error_chunks_count}块</span>
                                    </div>
                                    <div class="stats-row">
                                        <span class="stats-label">已校验块数:</span>
                                        <span class="stats-value">${chunks_verified}块</span>
                                    </div>
                                    <div class="stats-row">
                                        <span class="stats-label">完整性验证:</span>
                                        <span class="stats-value">${integrity_verified ? '通过' : '未通过或未提供预期哈希'}</span>
                                    </div>
                                </div>
                            `;
                            document.getElementById('statusDiv').innerHTML = fullStatsHtml;
                            document.getElementById('statusDiv').className = 'status completed';
                            
                            // 重置按钮状态
                            document.getElementById('uploadBtn').disabled = false;
                            document.getElementById('pauseBtn').disabled = true;
                            document.getElementById('resumeBtn').disabled = true;
                            document.getElementById('cancelBtn').disabled = true;
                            
                            showNotification('上传成功！', 'success');
                            
                            // 上传完成后清空选择的文件，允许选择新文件
                            resetUploadArea();
                        } else if (data.type === 'upload_error') {
                            // 隐藏上传进度信息
                            document.getElementById('uploadProgressInfo').style.display = 'none';
                            
                            document.getElementById('statusDiv').textContent = 
                                `上传失败: ${data.filename} - ${data.error}`;
                            document.getElementById('statusDiv').className = 'status error';
                            
                            // 重置按钮状态
                            document.getElementById('uploadBtn').disabled = false;
                            document.getElementById('pauseBtn').disabled = true;
                            document.getElementById('resumeBtn').disabled = false;
                            document.getElementById('cancelBtn').disabled = true;
                            
                            // 重置进度条
                            document.getElementById('progressFill').style.width = '0%';
                            document.getElementById('progressText').textContent = '0%';
                            document.getElementById('uploadSpeed').textContent = '0.00';
                            document.getElementById('remainingTime').textContent = '--:--:--';
                            document.getElementById('chunksReceived').textContent = '0';
                            document.getElementById('chunksVerified').textContent = '0';
                            
                            // 重置上传完成标志
                            uploadCompleted = false;
                            
                            showNotification('上传失败: ' + data.error, 'error');
                        } else if (data.type === 'upload_cancelled') {
                            // 隐藏上传进度信息
                            document.getElementById('uploadProgressInfo').style.display = 'none';
                            
                            document.getElementById('statusDiv').textContent = '上传已取消';
                            document.getElementById('statusDiv').className = 'status info';
                            
                            // 重置按钮状态
                            document.getElementById('uploadBtn').disabled = false;
                            document.getElementById('pauseBtn').disabled = true;
                            document.getElementById('resumeBtn').disabled = true;
                            document.getElementById('cancelBtn').disabled = true;
                            
                            // 重置进度条
                            document.getElementById('progressFill').style.width = '0%';
                            document.getElementById('progressText').textContent = '0%';
                            document.getElementById('uploadSpeed').textContent = '0.00';
                            document.getElementById('remainingTime').textContent = '--:--:--';
                            document.getElementById('chunksReceived').textContent = '0';
                            document.getElementById('chunksVerified').textContent = '0';
                            
                            // 重置上传完成标志
                            uploadCompleted = false;
                            
                            showNotification('上传已取消', 'info');
                        } else if (data.type === 'server_status') {
                            document.getElementById('serverInfo').innerHTML = 
                                `服务器信息: 连接客户端数 ${data.connected_clients}, ` +
                                `活跃上传数 ${data.active_uploads}, ` +
                                `总文件数 ${data.total_files}`;
                        } else if (data.type === 'resend_response') {
                            // 接收到重传的chunk数据
                            console.log('Received resend response:', data);
                        } else if (data.type === 'pong') {
                            // 心跳响应，无需处理
                        } else if (data.type === 'file_list_update') {
                            // 接收到文件列表更新
                            displayFileList(data.files);
                        } else if (data.type === 'download_response') {
                            // 处理下载响应
                            const { filename, download_url } = data;
                            // 直接发起下载
                            const link = document.createElement('a');
                            link.href = download_url;
                            link.download = filename;
                            document.body.appendChild(link);
                            link.click();
                            document.body.removeChild(link);
                        }
                    } catch (e) {
                        console.error('处理WebSocket消息出错:', e);
                    }
                };
                
                websocket.onclose = function(event) {
                    isConnected = false;
                    document.getElementById('connectionStatus').className = 'connection-status disconnected';
                    document.getElementById('connectionStatus').textContent = 'WebSocket: 已断开，正在重连...';
                    
                    // 5秒后重连
                    setTimeout(initWebSocket, 5000);
                };
                
                websocket.onerror = function(error) {
                    console.error('WebSocket错误:', error);
                    isConnected = false;
                    document.getElementById('connectionStatus').className = 'connection-status disconnected';
                    document.getElementById('connectionStatus').textContent = 'WebSocket: 连接错误';
                };
            } catch (e) {
                console.error('初始化WebSocket失败:', e);
                document.getElementById('connectionStatus').className = 'connection-status disconnected';
                document.getElementById('connectionStatus').textContent = 'WebSocket: 初始化失败';
            }
        }
        
        // 开始心跳检测
        function startHeartbeat() {
            setInterval(() => {
                if (websocket && websocket.readyState === WebSocket.OPEN) {
                    websocket.send(JSON.stringify({type: 'ping'}));
                }
            }, 30000); // 每30秒发送一次心跳
        }
        
        // 页面加载时初始化
        window.onload = function() {
            initWebSocket();
        };
        
        // 显示通知
        function showNotification(message, type = 'error') {
            // 移除已有的通知
            const existing = document.querySelector('.notification');
            if (existing) {
                existing.remove();
            }
            
            const notification = document.createElement('div');
            notification.className = `notification ${type}`;
            notification.textContent = message;
            document.body.appendChild(notification);
            
            // 3秒后自动移除通知
            setTimeout(() => {
                if (notification.parentNode) {
                    notification.remove();
                }
            }, 3000);
        }
        
        // 重置上传区域
        function resetUploadArea() {
            selectedFile = null;
            document.getElementById('fileName').textContent = '未选择文件';
            document.getElementById('fileDetails').textContent = '';
            document.getElementById('fileInfo').style.display = 'none';
            document.getElementById('uploadBtn').disabled = true;
            document.getElementById('pauseBtn').disabled = true;
            document.getElementById('resumeBtn').disabled = true;
            document.getElementById('cancelBtn').disabled = true;
            document.getElementById('progressFill').style.width = '0%';
            document.getElementById('progressText').textContent = '0%';
            document.getElementById('uploadSpeed').textContent = '0.00';
            document.getElementById('remainingTime').textContent = '--:--:--';
            document.getElementById('chunksReceived').textContent = '0';
            document.getElementById('chunksVerified').textContent = '0';
            document.getElementById('currentProgressText').textContent = '选择文件开始上传';
            document.getElementById('uploadProgressInfo').style.display = 'none';
            // 重置上传完成标志
            uploadCompleted = false;
        }
        
        // 拖拽上传功能
        const uploadArea = document.getElementById('uploadArea');
        uploadArea.addEventListener('dragover', (e) => {
            e.preventDefault();
            uploadArea.classList.add('dragover');
        });
        
        uploadArea.addEventListener('dragleave', () => {
            uploadArea.classList.remove('dragover');
        });
        
        uploadArea.addEventListener('drop', (e) => {
            e.preventDefault();
            uploadArea.classList.remove('dragover');
            const files = e.dataTransfer.files;
            if (files.length > 0) {
                handleFileSelect(files[0]);
            }
        });
        
        document.getElementById('fileInput').addEventListener('change', function(e) {
            if (e.target.files.length > 0) {
                handleFileSelect(e.target.files[0]);
            }
        });
        
        function handleFileSelect(file) {
            selectedFile = file;
            if (selectedFile) {
                document.getElementById('fileName').textContent = `已选择: ${selectedFile.name}`;
                document.getElementById('fileDetails').textContent = 
                    `${formatFileSize(selectedFile.size)} (${selectedFile.type || '未知类型'})`;
                
                // 计算预计分块数
                const chunkSize = 131072; // 128KB
                totalChunks = Math.ceil(selectedFile.size / chunkSize);
                document.getElementById('expectedChunks').textContent = totalChunks;
                
                document.getElementById('fileInfo').style.display = 'block';
                document.getElementById('uploadBtn').disabled = false;
            }
        }
        
        function formatFileSize(bytes) {
            if (bytes === 0) return '0 Bytes';
            const k = 1024;
            const sizes = ['Bytes', 'KB', 'MB', 'GB', 'TB'];
            const i = Math.floor(Math.log(bytes) / Math.log(k));
            return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
        }
        
        function formatTime(seconds) {
            const h = Math.floor(seconds / 3600);
            const m = Math.floor((seconds % 3600) / 60);
            const s = Math.floor(seconds % 60);
            return [h, m, s].map(v => v.toString().padStart(2, '0')).join(':');
        }
        
        async function startUpload() {
            if (!selectedFile) {
                alert('请先选择文件');
                return;
            }
            if (!isConnected) {
                alert('WebSocket未连接，请稍候重试');
                return;
            }
            
            // 计算文件哈希
            fileHash = await computeFileHash(selectedFile);
            const file_id = generateFileId(fileHash);
            
            // 显示上传进度信息
            document.getElementById('currentProgressText').textContent = `开始上传: ${selectedFile.name}`;
            document.getElementById('uploadProgressInfo').style.display = 'block';
            
            // 更新按钮状态
            document.getElementById('uploadBtn').disabled = true;
            document.getElementById('pauseBtn').disabled = false;
            document.getElementById('resumeBtn').disabled = true;
            document.getElementById('cancelBtn').disabled = false;
            
            isPaused = false;
            isCancelled = false;
            currentChunkIndex = 0;
            uploadedChunks.clear();
            
            // 计算每个chunk的哈希值
            await computeAllChunkHashes(selectedFile);
            
            // 开始上传
            uploadController = new AbortController();
            await uploadFileInChunks(selectedFile, file_id);
        }
        
        async function pauseUpload() {
            isPaused = true;
            if (uploadController) {
                uploadController.abort();
            }
            document.getElementById('currentProgressText').textContent = '上传已暂停';
            document.getElementById('uploadProgressInfo').style.display = 'block';
            document.getElementById('pauseBtn').disabled = true;
            document.getElementById('resumeBtn').disabled = false;
        }
        
        async function resumeUpload() {
            if (!selectedFile) return;
            
            isPaused = false;
            document.getElementById('currentProgressText').textContent = '继续上传文件...';
            document.getElementById('uploadProgressInfo').style.display = 'block';
            document.getElementById('pauseBtn').disabled = false;
            document.getElementById('resumeBtn').disabled = true;
            
            // 重新开始上传未完成的chunks
            uploadController = new AbortController();
            const fileHash = await computeFileHash(selectedFile);
            const file_id = generateFileId(fileHash);
            await uploadFileInChunks(selectedFile, file_id);
        }
        
        async function cancelUpload() {
            if (!selectedFile) return;
            
            isCancelled = true;
            if (uploadController) {
                uploadController.abort();
            }
            
            // 向服务器发送取消请求
            const fileHash = await computeFileHash(selectedFile);
            const file_id = generateFileId(fileHash);
            
            try {
                const response = await fetch(`/upload/cancel?file_id=${file_id}`, {
                    method: 'POST'
                });
                
                if (response.ok) {
                    console.log('Upload cancelled successfully');
                } else {
                    console.error('Failed to cancel upload');
                }
            } catch (error) {
                console.error('Error cancelling upload:', error);
            }
            
            // 重置状态
            document.getElementById('uploadBtn').disabled = false;
            document.getElementById('pauseBtn').disabled = true;
            document.getElementById('resumeBtn').disabled = true;
            document.getElementById('cancelBtn').disabled = true;
        }
        
        async function computeAllChunkHashes(file) {
            const chunkSize = 131072; // 128KB
            totalChunks = Math.ceil(file.size / chunkSize);
            
            for (let i = 0; i < totalChunks; i++) {
                const start = i * chunkSize;
                const end = Math.min(start + chunkSize, file.size);
                const chunk = file.slice(start, end);
                
                // 将Blob转换为ArrayBuffer再计算哈希
                const arrayBuffer = await chunk.arrayBuffer();
                const hash = sha256(arrayBuffer);
                chunkHashes[i] = hash;
            }
        }
        
        async function uploadFileInChunks(file, file_id) {
            const chunkSize = 131072; // 128KB
            totalChunks = Math.ceil(file.size / chunkSize);
            
            for (let i = 0; i < totalChunks; i++) {
                if (isPaused || isCancelled) {
                    currentChunkIndex = i;
                    return;
                }
                
                // 跳过已经上传的chunks
                if (uploadedChunks.has(i)) {
                    continue;
                }
                
                const start = i * chunkSize;
                const end = Math.min(start + chunkSize, file.size);
                const chunk = file.slice(start, end);
                
                const formData = new FormData();
                formData.append('file_id', file_id);
                formData.append('filename', file.name);
                formData.append('file_size', file.size);
                formData.append('chunk_index', i);
                formData.append('total_chunks', totalChunks);
                formData.append('chunk_data', chunk);
                formData.append('chunk_hash', chunkHashes[i]); // 添加chunk哈希
                formData.append('expected_file_hash', fileHash); // 添加完整文件哈希
                // 初始不设置覆盖相关参数，让服务器检测是否需要覆盖
                // formData.append('allow_overwrite', 'true');  
                // formData.append('overwrite_confirmed', 'true');  
                
                try {
                    const response = await fetch('/upload/chunk', {
                        method: 'POST',
                        body: formData,
                        signal: uploadController.signal
                    });
                    
                    const result = await response.json();
                    
                    if (response.ok) {
                        uploadedChunks.add(i);
                        document.getElementById('currentProgressText').textContent = 
                            `${file.name} - 第 ${i+1}/${totalChunks} 块`;
                    } else if (response.status === 409 && result.need_overwrite_confirm) {
                        // 文件已存在，需要确认覆盖
                        const confirmOverwrite = confirm(`文件 "${result.filename}" 已存在，大小为 ${formatFileSize(result.file_size)}，是否覆盖？\\n\\n当前目录文件列表:\\n${result.file_list.map(f => f.name + ' (' + f.size_formatted + ')').join('\\n')}`);
                        
                        if (confirmOverwrite) {
                            // 用户确认覆盖，重新发送请求，添加覆盖确认参数
                            const formDataOverwrite = new FormData();
                            formDataOverwrite.append('file_id', file_id);
                            formDataOverwrite.append('filename', file.name);
                            formDataOverwrite.append('file_size', file.size);
                            formDataOverwrite.append('chunk_index', i);
                            formDataOverwrite.append('total_chunks', totalChunks);
                            formDataOverwrite.append('chunk_data', chunk);
                            formDataOverwrite.append('chunk_hash', chunkHashes[i]); // 添加chunk哈希
                            formDataOverwrite.append('expected_file_hash', fileHash); // 添加完整文件哈希
                            formDataOverwrite.append('allow_overwrite', 'true');
                            formDataOverwrite.append('overwrite_confirmed', 'true');  // 确认覆盖
                            
                            const overwriteResponse = await fetch('/upload/chunk', {
                                method: 'POST',
                                body: formDataOverwrite,
                                signal: uploadController.signal
                            });
                            
                            const overwriteResult = await overwriteResponse.json();
                            
                            if (overwriteResponse.ok) {
                                uploadedChunks.add(i);
                                document.getElementById('currentProgressText').textContent = 
                                    `${file.name} - 第 ${i+1}/${totalChunks} 块`;
                            } else {
                                document.getElementById('currentProgressText').textContent = 
                                    `上传失败: ${overwriteResult.error}`;
                                
                                document.getElementById('uploadBtn').disabled = false;
                                document.getElementById('pauseBtn').disabled = true;
                                document.getElementById('resumeBtn').disabled = false;
                                document.getElementById('cancelBtn').disabled = true;
                                
                                // 重置进度条
                                document.getElementById('progressFill').style.width = '0%';
                                document.getElementById('progressText').textContent = '0%';
                                document.getElementById('uploadSpeed').textContent = '0.00';
                                document.getElementById('remainingTime').textContent = '--:--:--';
                                document.getElementById('chunksReceived').textContent = '0';
                                document.getElementById('chunksVerified').textContent = '0';
                                
                                // 重置上传完成标志
                                uploadCompleted = false;
                                
                                showNotification('上传失败: ' + overwriteResult.error, 'error');
                                return;
                            }
                        } else {
                            // 用户取消覆盖，停止上传
                            document.getElementById('currentProgressText').textContent = '上传已取消（用户取消覆盖）';
                            
                            document.getElementById('uploadBtn').disabled = false;
                            document.getElementById('pauseBtn').disabled = true;
                            document.getElementById('resumeBtn').disabled = true;
                            document.getElementById('cancelBtn').disabled = true;
                            
                            // 重置进度条
                            document.getElementById('progressFill').style.width = '0%';
                            document.getElementById('progressText').textContent = '0%';
                            document.getElementById('uploadSpeed').textContent = '0.00';
                            document.getElementById('remainingTime').textContent = '--:--:--';
                            document.getElementById('chunksReceived').textContent = '0';
                            document.getElementById('chunksVerified').textContent = '0';
                            
                            // 重置上传完成标志
                            uploadCompleted = false;
                            
                            showNotification('上传已取消（用户取消覆盖）', 'info');
                            return;
                        }
                    } else {
                        document.getElementById('currentProgressText').textContent = 
                            `上传失败: ${result.error}`;
                        document.getElementById('uploadBtn').disabled = false;
                        document.getElementById('pauseBtn').disabled = true;
                        document.getElementById('resumeBtn').disabled = false;
                        document.getElementById('cancelBtn').disabled = true;
                        
                        // 重置进度条
                        document.getElementById('progressFill').style.width = '0%';
                        document.getElementById('progressText').textContent = '0%';
                        document.getElementById('uploadSpeed').textContent = '0.00';
                        document.getElementById('remainingTime').textContent = '--:--:--';
                        document.getElementById('chunksReceived').textContent = '0';
                        document.getElementById('chunksVerified').textContent = '0';
                        
                        // 重置上传完成标志
                        uploadCompleted = false;
                        
                        showNotification('上传失败: ' + result.error, 'error');
                        return;
                    }
                } catch (error) {
                    if (error.name === 'AbortError') {
                        // 上传被暂停或取消
                        return;
                    }
                    document.getElementById('currentProgressText').textContent = 
                        `上传失败: ${error.message}`;
                    document.getElementById('uploadBtn').disabled = false;
                    document.getElementById('pauseBtn').disabled = true;
                    document.getElementById('resumeBtn').disabled = false;
                    document.getElementById('cancelBtn').disabled = true;
                    
                    // 重置进度条
                    document.getElementById('progressFill').style.width = '0%';
                    document.getElementById('progressText').textContent = '0%';
                    document.getElementById('uploadSpeed').textContent = '0.00';
                    document.getElementById('remainingTime').textContent = '--:--:--';
                    document.getElementById('chunksReceived').textContent = '0';
                    document.getElementById('chunksVerified').textContent = '0';
                    
                    // 重置上传完成标志
                    uploadCompleted = false;
                    
                    showNotification('上传失败: ' + error.message, 'error');
                    return;
                }
            }
        }
        
        async function computeFileHash(file) {
            // 使用FileReader读取整个文件并计算哈希
            return new Promise((resolve, reject) => {
                const reader = new FileReader();
                reader.onload = function(e) {
                    // 将ArrayBuffer转换为Uint8Array，然后传递给sha256
                    const arrayBuffer = e.target.result;
                    const hash = sha256(arrayBuffer);
                    resolve(hash);
                };
                reader.onerror = reject;
                reader.readAsArrayBuffer(file);
            });
        }
        
        function generateFileId(hash) {
            return hash.substring(0, 16);
        }
        
        // 显示文件列表
        function displayFileList(files) {
            const tbody = document.getElementById('fileListBody');
            if (files.length === 0) {
                tbody.innerHTML = '<tr><td colspan="5">暂无文件</td></tr>';
                return;
            }
            
            let html = '';
            files.forEach(file => {
                const date = new Date(file.modified * 1000);
                const formattedDate = date.toLocaleString();
                html += `
                <tr>
                    <td class="file-name-cell" title="${file.name}">${file.name || ''}</td>
                    <td>${file.size_formatted}</td>
                    <td>${formattedDate}</td>
                    <td class="hash-cell" title="${file.hash}">${file.hash || ''}</td>
                    <td><button class="download-btn" onclick="downloadFile('${file.name}')">下载</button></td>
                </tr>`;
            });
            tbody.innerHTML = html;
        }
        
        // 下载文件
        function downloadFile(filename) {
            if (isConnected && websocket && websocket.readyState === WebSocket.OPEN) {
                // 通过WebSocket发送下载请求
                websocket.send(JSON.stringify({
                    type: 'download_file',
                    filename: filename
                }));
            } else {
                // WebSocket不可用时，直接发起下载请求
                const link = document.createElement('a');
                link.href = `/download?filename=${encodeURIComponent(filename)}`;
                link.download = filename;
                document.body.appendChild(link);
                link.click();
                document.body.removeChild(link);
            }
        }
        
        // 转义HTML特殊字符，防止XSS
        function escapeHtml(str) {
            return str.replace(/[&<>"']/g, m => ({
                '&': '&amp;',
                '<': '&lt;',
                '>': '&gt;',
                '"': '&quot;',
                "'": '&#039;'
            })[m]);
        }
    </script>
</body>
</html>
"""
        return web.Response(text=html_content, content_type='text/html')

def create_app():
    server = FileUploadServer()
    
    app = web.Application()
    app.add_routes([
        web.get('/', server.handle_index),
        web.post('/upload/chunk', server.handle_chunk_upload),
        web.post('/upload/resumable', server.handle_resumable_upload),
        web.post('/upload/cancel', server.handle_cancel_upload),
        web.get('/upload/status', server.handle_upload_status),
        web.get('/upload/verify', server.handle_verify_integrity),  # 新增完整性验证接口
        web.get('/ws', server.handle_websocket),
        web.get('/files/list', server.handle_list_files),
        web.get('/download', server.handle_download),  # 新增下载接口
    ])
    
    return app

if __name__ == '__main__':
    app = create_app()
    # 在IPv6上运行
    web.run_app(app, host='::', port=8080)



