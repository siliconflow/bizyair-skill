"""oss_multipart.py — OSS multipart upload + resume (STS temporary credentials, OSS v1 signing).

A single PutObject is capped at 5 GB, so large models must be split into parts. This module
depends only on the standard library:
  - Adaptive part size (16 MB by default, scaled up for the 9000-part cap)
  - Uploads with 3 concurrent streams; a failed part is re-sent alone
  - A checkpoint is persisted per file signature, so an interrupted process can resume
  - When the STS expires, credentials are refreshed and the upload continues (same uploadId)
  - Upload progress callback (bytes sent / total bytes)
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import http.client
import json
import os
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.utils import formatdate
from pathlib import Path
from typing import Any, Callable
from xml.etree import ElementTree
from xml.sax.saxutils import escape as xml_escape

import paths

MIN_PART_SIZE = 5 * 1024 * 1024
DEFAULT_PART_SIZE = 16 * 1024 * 1024
MAX_PART_SIZE = 512 * 1024 * 1024
MAX_PARTS = 9000
DEFAULT_CONCURRENCY = 6
CHECKPOINT_VERSION = 1


class MultipartError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0, body: str = '') -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class _OssError(MultipartError):
    pass


def part_size_for(size: int) -> int:
    """Compute the part size for OSS's 10000-part cap, at least 5 MB."""
    if size <= 0:
        return DEFAULT_PART_SIZE
    needed = (size + MAX_PARTS - 1) // MAX_PARTS
    part = max(DEFAULT_PART_SIZE, needed)
    part = ((part + 1024 * 1024 - 1) // (1024 * 1024)) * 1024 * 1024
    return max(MIN_PART_SIZE, min(part, MAX_PART_SIZE))


def _local(tag: str) -> str:
    return tag.rsplit('}', 1)[-1]


def _xml_text(body: str, name: str) -> str | None:
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        return None
    for node in root.iter():
        if _local(node.tag) == name:
            return node.text or ''
    return None


def checkpoint_path(signature: str, folder: str | Path | None = None) -> Path:
    root = (Path(folder) / 'model_uploads') if folder else (paths.resolve_runtime_root() / 'model_uploads')
    return root / f'{signature[:40]}.json'


def load_checkpoint(path: Path, *, size: int, mtime_ns: int, object_key: str | None = None) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None
    if not isinstance(data, dict) or data.get('version') != CHECKPOINT_VERSION:
        return None
    if data.get('size') != size or data.get('mtime_ns') != mtime_ns:
        return None
    if object_key is not None and data.get('object_key') != object_key:
        return None
    if not data.get('upload_id'):
        return None
    return data


def save_checkpoint(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(data, version=CHECKPOINT_VERSION, updated_at=int(time.time()))
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    try:
        os.chmod(temp, 0o600)
    except OSError:
        pass
    os.replace(temp, path)


def delete_checkpoint(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _parse_expiration(value: object) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except Exception:
        return None


def _checkpoint_still_valid(checkpoint: dict[str, Any]) -> bool:
    """Whether the STS stored in the checkpoint is still usable; no expiry time means usable."""
    expiration = _parse_expiration(checkpoint.get('expiration'))
    if expiration is None:
        return True
    return expiration > dt.datetime.now(dt.timezone.utc)


def _checkpoint_token(checkpoint: dict[str, Any]) -> dict[str, Any]:
    return {
        'file': {
            'object_key': checkpoint.get('object_key'),
            'access_key_id': checkpoint.get('access_key_id'),
            'access_key_secret': checkpoint.get('access_key_secret'),
            'security_token': checkpoint.get('security_token'),
            'expiration': checkpoint.get('expiration'),
        },
        'storage': {'bucket': checkpoint.get('bucket'), 'endpoint': checkpoint.get('endpoint')},
    }


class _Credentials:
    def __init__(self, token_inner: dict[str, Any], refresh: Callable[[], dict[str, Any]] | None = None) -> None:
        self.lock = threading.Lock()
        self.refresh = refresh
        self._apply(token_inner)

    def _apply(self, token_inner: dict[str, Any]) -> None:
        file_info = token_inner.get('file') or {}
        storage = token_inner.get('storage') or {}
        for field in ('object_key', 'access_key_id', 'access_key_secret', 'security_token'):
            value = file_info.get(field)
            if not value:
                raise MultipartError(f'Upload credentials missing {field}')
        for field in ('bucket', 'endpoint'):
            if not storage.get(field):
                raise MultipartError(f'Upload credentials missing storage.{field}')
        self.file_info = file_info
        self.storage = storage
        self.access_key_id = str(file_info['access_key_id'])
        self.access_key_secret = str(file_info['access_key_secret'])
        self.security_token = str(file_info['security_token'])
        self.object_key = str(file_info['object_key'])
        self.bucket = str(storage['bucket'])
        self.endpoint = str(storage['endpoint'])
        self.session_key: str | None = None

    def lock_session(self, object_key: str | None = None) -> None:
        """Pin the object_key for this multipart session; resume/credential refresh never change it again."""
        self.object_key = object_key or self.object_key
        self.session_key = self.object_key

    def snapshot(self) -> dict[str, str]:
        with self.lock:
            return {'access_key_id': self.access_key_id, 'access_key_secret': self.access_key_secret,
                    'security_token': self.security_token, 'object_key': self.object_key,
                    'bucket': self.bucket, 'endpoint': self.endpoint}

    def renew(self) -> bool:
        if not self.refresh:
            return False
        with self.lock:
            try:
                token_inner = self.refresh()
            except Exception:
                return False
            self._apply(token_inner)
            if self.session_key:
                self.object_key = self.session_key
            return True


def _signature(secret: str, method: str, content_type: str, date: str, token: str, resource: str) -> str:
    canonical_headers = f'x-oss-security-token:{token}\n'
    string_to_sign = f'{method}\n\n{content_type}\n{date}\n{canonical_headers}{resource}'
    digest = hmac.new(secret.encode('utf-8'), string_to_sign.encode('utf-8'), hashlib.sha1).digest()
    return base64.b64encode(digest).decode('ascii')


def _oss_call(creds: _Credentials, method: str, suffix: str, *, content_type: str = '',
              body: bytes | None = None, file_path: str | None = None,
              offset: int = 0, length: int = 0, timeout: int = 1200) -> tuple[int, dict[str, str], str]:
    snap = creds.snapshot()
    object_path = '/' + urllib.parse.quote(snap['object_key'], safe='/')
    resource = f"/{snap['bucket']}/{snap['object_key']}{suffix}"
    date = formatdate(usegmt=True)
    signature = _signature(snap['access_key_secret'], method, content_type, date, snap['security_token'], resource)
    connection = http.client.HTTPSConnection(f"{snap['bucket']}.{snap['endpoint']}", timeout=timeout)
    try:
        connection.putrequest(method, object_path + suffix)
        connection.putheader('Date', date)
        if content_type:
            connection.putheader('Content-Type', content_type)
        connection.putheader('x-oss-security-token', snap['security_token'])
        connection.putheader('Authorization', f"OSS {snap['access_key_id']}:{signature}")
        total = len(body) if body is not None else length
        connection.putheader('Content-Length', str(total))
        connection.endheaders()
        if body is not None:
            connection.send(body)
        elif file_path:
            remaining = length
            with open(file_path, 'rb') as handle:
                handle.seek(offset)
                while remaining > 0:
                    chunk = handle.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise MultipartError('File ended early while reading a part (was the file modified?)')
                    connection.send(chunk)
                    remaining -= len(chunk)
        response = connection.getresponse()
        text = response.read().decode('utf-8', errors='replace')
        headers = {str(k).lower(): str(v) for k, v in response.getheaders()}
        return response.status, headers, text
    finally:
        connection.close()


def _oss_name(upload_id: str) -> str:
    return urllib.parse.quote(upload_id, safe='')


def _retry(creds: _Credentials, fn: Callable[[], tuple[int, dict[str, str], str]], *, attempts: int = 5) -> tuple[int, dict[str, str], str]:
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except _OssError as exc:
            last = exc
            if exc.status in (400, 403) and creds.renew():
                continue
            if exc.status >= 500 or exc.status in (408, 429):
                time.sleep(min(8, 1 + attempt * 2))
                continue
            raise
        except (OSError, http.client.HTTPException) as exc:
            last = exc
            time.sleep(min(8, 1 + attempt * 2))
    raise MultipartError(f'OSS request kept failing: {last}')


def _initiate(creds: _Credentials) -> str:
    status, _, body = _retry(creds, lambda: _oss_call(creds, 'POST', '?uploads'))
    if status not in (200, 201):
        raise _OssError(f'Failed to start multipart upload: HTTP {status} {body[:200]}', status=status, body=body)
    upload_id = _xml_text(body, 'UploadId')
    if not upload_id:
        raise _OssError('Failed to start multipart upload: no UploadId in the response', status=status, body=body)
    return upload_id


def _upload_part(creds: _Credentials, upload_id: str, number: int, file_path: str, offset: int, length: int) -> str:
    suffix = f'?partNumber={number}&uploadId={_oss_name(upload_id)}'
    status, headers, body = _retry(creds, lambda: _oss_call(
        creds, 'PUT', suffix, content_type='application/octet-stream',
        file_path=file_path, offset=offset, length=length))
    if status not in (200, 201):
        raise _OssError(f'Failed to upload part {number}: HTTP {status} {body[:200]}', status=status, body=body)
    etag = headers.get('etag')
    if not etag:
        raise _OssError(f'Failed to upload part {number}: no ETag in the response', status=status, body=body)
    return etag


def _complete(creds: _Credentials, upload_id: str, parts: dict[int, str]) -> None:
    pieces = ''.join(
        f'<Part><PartNumber>{number}</PartNumber><ETag>{xml_escape(parts[number])}</ETag></Part>'
        for number in sorted(parts)
    )
    body = ('<CompleteMultipartUpload>' + pieces + '</CompleteMultipartUpload>').encode('utf-8')
    suffix = f'?uploadId={_oss_name(upload_id)}'
    status, _, text = _retry(creds, lambda: _oss_call(
        creds, 'POST', suffix, content_type='application/xml', body=body))
    if status not in (200, 201) or '<Error>' in text:
        raise _OssError(f'Failed to complete multipart upload: HTTP {status} {text[:300]}', status=status, body=text)


def upload_model_file(file_path: str, token_inner: dict[str, Any], *,
                      token_refresh: Callable[[], dict[str, Any]] | None = None,
                      concurrency: int = DEFAULT_CONCURRENCY,
                      progress: Callable[[int, int], None] | None = None,
                      restart: bool = False,
                      checkpoint_dir: str | Path | None = None,
                      checkpoint_name: str | None = None) -> dict[str, Any]:
    """Upload the whole file; returns object_key / upload_id / resumed_parts / restarted."""
    path = Path(file_path)
    stat = path.stat()
    size = stat.st_size
    part_size = part_size_for(size)
    total_parts = (size + part_size - 1) // part_size
    key = checkpoint_name or hashlib.sha256(str(path.resolve()).encode('utf-8')).hexdigest()
    cp_path = checkpoint_path(key, checkpoint_dir)
    checkpoint = None if restart else load_checkpoint(
        cp_path, size=size, mtime_ns=stat.st_mtime_ns)
    if checkpoint and checkpoint.get('access_key_id') and _checkpoint_still_valid(checkpoint):
        # Key point: a new token yields a new temporary object_key, so an old session can only resume
        # with the unexpired STS stored in the checkpoint.
        creds = _Credentials(_checkpoint_token(checkpoint))
        creds.lock_session(str(checkpoint.get('object_key') or creds.object_key))
    else:
        if checkpoint:
            delete_checkpoint(cp_path)
        checkpoint = None
        creds = _Credentials(token_inner, token_refresh)
        creds.lock_session()
    if checkpoint and checkpoint.get('part_size'):
        part_size = int(checkpoint['part_size'])
        total_parts = (size + part_size - 1) // part_size
    if checkpoint:
        upload_id = str(checkpoint['upload_id'])
        parts = {int(k): str(v) for k, v in (checkpoint.get('parts') or {}).items()}
    else:
        upload_id = _initiate(creds)
        parts = {}
    resumed_parts = len(parts)
    if checkpoint and checkpoint.get('completed'):
        return {'object_key': creds.object_key, 'upload_id': upload_id,
                'resumed_parts': resumed_parts, 'total_parts': total_parts,
                'part_size': part_size, 'size': size, 'restarted': False,
                'completed': True, 'checkpoint_path': str(cp_path)}

    state = {'upload_id': upload_id, 'part_size': part_size, 'parts': parts,
             'signature': checkpoint.get('signature') if checkpoint else '', 'size': size,
             'mtime_ns': stat.st_mtime_ns, 'object_key': creds.object_key,
             'access_key_id': creds.access_key_id, 'access_key_secret': creds.access_key_secret,
             'security_token': creds.security_token, 'bucket': creds.bucket, 'endpoint': creds.endpoint,
             'expiration': checkpoint.get('expiration') if checkpoint else
             str((token_inner.get('file') or {}).get('expiration') or '')}
    save_checkpoint(cp_path, state)

    uploaded = sum(min(part_size, size - (number - 1) * part_size) for number in parts if number <= total_parts)
    if progress:
        progress(uploaded, size)

    missing = [number for number in range(1, total_parts + 1) if number not in parts]
    lock = threading.Lock()

    def one(number: int) -> tuple[int, str]:
        offset = (number - 1) * part_size
        length = min(part_size, size - offset)
        etag = _upload_part(creds, upload_id, number, str(path), offset, length)
        return number, etag

    dirty = {'count': 0, 'saved_at': time.time()}

    def record(number: int, etag: str) -> None:
        nonlocal uploaded
        with lock:
            if number in state['parts']:
                return
            state['parts'][number] = etag
            uploaded += min(part_size, size - (number - 1) * part_size)
            dirty['count'] += 1
            if dirty['count'] >= 8 or time.time() - dirty['saved_at'] >= 5:
                save_checkpoint(cp_path, state)
                dirty['count'] = 0
                dirty['saved_at'] = time.time()
            if progress:
                progress(uploaded, size)

    workers = max(1, min(8, int(concurrency or DEFAULT_CONCURRENCY)))
    if missing:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(one, number): number for number in missing}
            try:
                for future in as_completed(futures):
                    number, etag = future.result()
                    record(number, etag)
            except _OssError as exc:
                for future in futures:
                    future.cancel()
                # When resuming, the old object_key may no longer be authorized by the new STS:
                # drop the checkpoint and restart once from scratch.
                if checkpoint and exc.status in (403, 404):
                    delete_checkpoint(cp_path)
                    return upload_model_file(file_path, token_inner, token_refresh=token_refresh,
                                             concurrency=concurrency, progress=progress, restart=True,
                                             checkpoint_dir=checkpoint_dir, checkpoint_name=checkpoint_name) | {'restarted': True}
                raise
            except Exception:
                for future in futures:
                    future.cancel()
                raise

    save_checkpoint(cp_path, state)
    try:
        _complete(creds, upload_id, {int(k): v for k, v in state['parts'].items()})
        state['completed'] = True
        save_checkpoint(cp_path, state)
    except _OssError as exc:
        if 'NoSuchUpload' in exc.body and not restart and checkpoint:
            delete_checkpoint(cp_path)
            return upload_model_file(file_path, token_inner, token_refresh=token_refresh,
                                     concurrency=concurrency, progress=progress, restart=True,
                                     checkpoint_dir=checkpoint_dir, checkpoint_name=checkpoint_name) | {'restarted': True}
        raise
    return {'object_key': creds.object_key, 'upload_id': upload_id,
            'resumed_parts': resumed_parts, 'total_parts': total_parts,
            'part_size': part_size, 'size': size, 'restarted': False,
            'completed': True, 'checkpoint_path': str(cp_path)}
