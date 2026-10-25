# 备用方案：未合并的上游 PR

本文档记录 vitiko98/qobuz-dl 上**没有合进本 fork** 的 PR 里有价值的实现，以备将来需要时使用。
这里的代码**目前都不在项目里**；需要时按对应章节移植。

> 记录时间：2026-10-26。本 fork 当时的状态：合了 #185 #235 #236 #302 #303 #307 #308，
> 挑了 #326 的 `downloader.py` 重试和 #331 的两个日期修复，登录用自己的 UAT 刷新方案（`qopy.Client.auth`）。

## 获取原始代码

上游 PR 的 ref 会一直保留在 GitHub 上，即使作者删了 fork：

```bash
git fetch upstream pull/306/head:refs/pr/306   # 其他 PR 把编号换掉
git diff 9c8901d refs/pr/306                   # 9c8901d = 上游 master 最后一个提交
```

记录时各 PR 的最新提交（PR 以后若有更新，以这些为准对照本文档）：

| PR | 最新提交 | 作者 | 主题 |
|---|---|---|---|
| #306 | `a1503d0f55` | lcorbasson | 分段下载、dry-run、verbose 等合集 |
| #334 | `00f8ce21b2` | guyathomas | user_id + token 登录，安全加固，测试 |
| #331 | `7ac828b191` | paulborile | OAuth 登录 |
| #326 | `0e150c0c1f` | Zachery2008 | CDN 重试 + 登录改写 |
| #282 | `e779d8ea98` | KMohZaid | 早期 token 登录（已过时） |

---

## 1. 分段下载 / 新签名方案（#306）—— 迫不得已时再用

### 什么时候需要

出现以下任一情况，说明现在的 `track/getFileUrl` 直连下载已经不行了：

- `cfg_setup()` 报 `Can't find any valid app secret`，`-r` 重新抓 bundle 也没用
- `track/getFileUrl` 返回 400 / 403，或者返回的 JSON 里没有 `url` 字段
- 直连 URL 下载被 CDN（Akamai）拒绝，但网页播放器还能正常播放

#334 的作者在 2026-04 说过：Qobuz 正在把下载请求的签名从 MD5 迁移到 **SHA-256 + HKDF**，部分账号已经失效。#306 是唯一实现了新方案的 PR。

### 原理

模拟 play.qobuz.com 网页播放器的取流方式：

```
1. POST session/start  (profile=qbz-1, 签名用 _modern_sig)
      → 返回 session_id 和 infos ("<salt>.<info>", base64url)
2. 会话密钥 = HKDF-SHA256(key=bytes.fromhex(app_secret), salt, info, length=16)
   之后所有请求带请求头 X-Session-Id
3. GET file/url  (track_id, format_id, intent=import, 签名用 _modern_sig)
      → 返回 url_template（含 $SEGMENT$ 占位符）、n_segments、key、bits_depth、sampling_rate(Hz)
4. 曲目密钥 = AES-128-CBC 解密 key 字段 ("<?>.<wrapped>.<iv>")，PKCS7 去填充
5. 依次下载第 0..n_segments 段（共 n_segments+1 段，是 fMP4 分片）
      → 从第 1 段找到 uuid box 的 16 字节 UUID
      → 每段里匹配该 UUID 的 uuid box 列出了各帧的长度/标志/计数器，
        对加密帧用 AES-CTR(曲目密钥, 计数器补零到 16 字节) 原地解密
6. 拼接成 .mp4，用 ffmpeg -c:a copy -f flac 无损转封装成 FLAC
```

MP3（quality 5）在 #306 里仍然走 `track/getFileUrl`，作者说直连对 MP3 一直可用；`file/url` 只接受 6 / 7 / 27。

### 依赖

- Python 包 `cryptography`（加进 `requirements.txt` 和 `setup.py`；#306 在 requirements 里写了两遍，别照抄）
- 系统里要有 **ffmpeg**（只有分段模式需要）

### 代码 A：`qopy.py` 部分（出自 #306，原样）

导入和 `__init__` 新增的状态：

```python
import base64
from cryptography.hazmat.primitives import hashes, padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# Client.__init__ 里，在 self.auth(...) 之前：
self.session_id = None
self.session_infos = None
self.session_key = None
```

`api_call` 里新增的两个端点分支（放在 `else: params = kwargs` 前面）：

```python
        elif epoint == "session/start":
            params = {"profile": "qbz-1"}
            params["request_ts"] = int(time.time())
            params["request_sig"] = self._modern_sig(
                epoint, params, kwargs.get("sec", self.sec)
            )
        elif epoint == "file/url":
            track_id = kwargs["id"]
            fmt_id = kwargs["fmt_id"]
            if int(fmt_id) not in (6, 7, 27):
                raise InvalidQuality("Invalid quality id: choose between 6, 7 or 27")
            params = {
                "track_id": track_id,
                "format_id": fmt_id,
                "intent": "import",
            }
            params["request_ts"] = int(time.time())
            params["request_sig"] = self._modern_sig(
                epoint, params, kwargs.get("sec", self.sec)
            )
```

`session/start` 必须用 POST 表单发送（发请求的地方加一个分支）：

```python
        elif epoint == "session/start":
            r = self.session.post(
                self.base + epoint,
                data=params,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
```

另外把 `"file/url"` 加进“400 → `InvalidAppSecretError`”的端点列表。

签名与密钥工具方法：

```python
    def _modern_sig(self, epoint, params, sec):
        object_, method = epoint.split("/")
        r_sig = [object_, method]
        for key in sorted(params):
            value = params[key]
            if key not in ("request_ts", "request_sig") and isinstance(
                value, (str, int, float)
            ):
                r_sig.extend((key, str(value)))
        r_sig.extend((str(params["request_ts"]), sec))
        return hashlib.md5("".join(r_sig).encode("utf-8")).hexdigest()

    @staticmethod
    def _b64url_decode(value):
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

    def _derive_session_key(self):
        salt, info = self.session_infos.split(".")
        return HKDF(
            algorithm=hashes.SHA256(),
            length=16,
            salt=self._b64url_decode(salt),
            info=self._b64url_decode(info),
        ).derive(bytes.fromhex(self.sec))

    def _unwrap_track_key(self, key_token):
        _, wrapped, iv = key_token.split(".")
        decryptor = Cipher(
            algorithms.AES(self.session_key),
            modes.CBC(self._b64url_decode(iv)),
        ).decryptor()
        padded = decryptor.update(self._b64url_decode(wrapped)) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        return unpadder.update(padded) + unpadder.finalize()
```

> 注意 `_modern_sig` 本身还是 MD5，只是改成了按参数名排序拼接；SHA-256 只用在 HKDF 派生会话密钥上。
> `bytes.fromhex(self.sec)` 要求 app secret 是十六进制字符串，如果将来 bundle 里的 secret 格式变了，这里会先报错。

`get_track_url` 替换为“先直连、失败再分段”：

```python
    def get_track_url(self, id, fmt_id, force_segments=False):
        time.sleep(1)  # brief pause between tracks to avoid CDN rate limiting
        # Quick fallback for MP3 where direct URLs always seem to work
        if int(fmt_id) == 5:
            track = self.api_call("track/getFileUrl", id=id, fmt_id=fmt_id)
            return track
        # Try the direct URL first if downloading by segments is not forced
        if not force_segments:
            try:
                track = self.api_call("track/getFileUrl", id=id, fmt_id=fmt_id)
                if "url" in track:
                    return track
            except Exception:
                pass  # Direct URL failed (e.g. Akamai block), move to segmented method
        # Failsafe, segmented (Web Player) method
        if self.session_id is None:
            session = self.api_call("session/start")
            self.session_id = session["session_id"]
            self.session_infos = session["infos"]
            self.session_key = self._derive_session_key()
            self.session.headers.update({"X-Session-Id": self.session_id})
        track = self.api_call("file/url", id=id, fmt_id=fmt_id)
        if "bits_depth" in track and "bit_depth" not in track:
            track["bit_depth"] = track["bits_depth"]
        if track.get("sampling_rate", 0) > 1000:
            track["sampling_rate"] = track["sampling_rate"] / 1000
        if "key" in track:
            track["raw_key"] = self._unwrap_track_key(track["key"])
        return track
```

> 本 fork 的 `downloader.py` 已经在曲目之间 `time.sleep(1)`（来自 #326）。移植时**删掉其中一处**，否则每首停 2 秒。

### 代码 B：`downloader.py` 部分（出自 #306，原样）

新增导入：

```python
import collections
import datetime
import subprocess
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
```

`tqdm_download` 整体替换。参数 `url` 既可以是字符串，也可以是 `get_track_url` 返回的整个字典；返回值改成 `done`（成功与否），**不再抛异常**：

```python
def tqdm_download(url, fname, desc, duration=None, playback_speed=1.0):
    done = False
    segments = 0
    # `url` can be a string or a dict, let's sort this out
    if isinstance(url, collections.abc.Mapping):
        track_url_dict = url
        if "url" in track_url_dict:
            url = track_url_dict["url"]
            logger.debug(f"{OFF+YELLOW}Downloading from URL: {url}")
        else:
            # Fallback: we'll download using segments (emulating the Web Player)
            url = track_url_dict["url_template"]
            segments = track_url_dict['n_segments'] + 1
            logger.debug(f"{OFF+YELLOW}Downloading {segments} segments using URL template: {url}")
    else:
        logger.debug(f"{OFF+YELLOW}Downloading from URL: {url}")

    try:
        duration_str = "≤" + str(datetime.timedelta(seconds=duration)) + f"/{playback_speed:0.1f}"
    except TypeError:
        duration_str = ""
    tmp_fname = fname + ".mp4"
    headers = {
        'User-Agent': USER_AGENT,
    }

    r = None
    try:
        tic = time.perf_counter()
        if segments == 0:
            r = requests.get(
                url,
                allow_redirects=True,
                stream=True,
                headers=headers,
                timeout=(10, 60),
            )
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
        else:
            segment_uuid = None
            total = 0
            for segment in range(segments):
                r = requests.head(
                    url.replace("$SEGMENT$", str(segment)),
                    allow_redirects=True,
                    headers=headers,
                )
                r.raise_for_status()
                total += int(r.headers.get("content-length", 0))
                r.close()

        download_size = 0
        with open(tmp_fname, "wb") as file, tqdm(
            total=total,
            unit="iB",
            unit_scale=True,
            unit_divisor=1024,
            desc=desc,
            bar_format=CYAN + "{n_fmt}/{total_fmt} /// {desc}"
                + "        "
                + "[{elapsed}+{remaining}" + duration_str + ", {rate_fmt}{postfix}]",
        ) as bar:
            if segments == 0:
                for data in r.iter_content(chunk_size=1024):
                    r.raise_for_status()
                    size = file.write(data)
                    bar.update(size)
                    download_size += size
            else:
                for segment in range(segments):
                    r = requests.get(
                        url.replace("$SEGMENT$", str(segment)),
                        allow_redirects=True,
                        stream=True,
                        headers=headers,
                    )
                    r.raise_for_status()
                    segment_total = int(r.headers.get("content-length", 0))
                    segment_size = 0
                    segment_data = bytearray()
                    for data in r.iter_content(chunk_size=1024):
                        r.raise_for_status()
                        segment_data.extend(data)
                        size = len(data)
                        bar.update(size)
                        segment_size += size
                    download_size += segment_size
                    r.close()

                    if segment_total and segment_total != segment_size:
                        raise ConnectionError("File download was interrupted for " + fname)
                    if segment == 1:
                        segment_uuid = _get_qobuz_segment_uuid(segment_data)
                        if segment_uuid is None:
                            raise requests.exceptions.ConnectionError(
                                "Cannot find Qobuz segment UUID for " + fname
                            )
                    file.write(
                        _decrypt_qobuz_segment(
                            segment_data, track_url_dict["raw_key"], segment_uuid
                        )
                    )

        toc = time.perf_counter()
        elapsed_time = (toc - tic)
        if duration is not None:
            waiting_time = max(0, duration / playback_speed - elapsed_time)
            logger.debug(f"{OFF+YELLOW}Sleeping {waiting_time:0.0f} seconds to match playback time")
            time.sleep(waiting_time)

        if segments == 0:
            if download_size < total:
                raise ConnectionError(
                    f"Incomplete download ({download_size}/{total} bytes) for {fname}"
                )
            os.rename(tmp_fname, fname)
        else:
            remux = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", tmp_fname, "-c:a", "copy", "-f", "flac", fname], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            if remux.returncode != 0:
                raise requests.exceptions.ConnectionError(
                    "File remux failed for {}: {}".format(
                        fname, remux.stderr.strip() or "ffmpeg exited with an error"
                    )
                )
        done = True

    finally:
        if r:
            r.close()
        if os.path.isfile(tmp_fname):
            os.remove(tmp_fname)
        return done


def _get_qobuz_segment_uuid(segment_data):
    pos = 0
    while pos + 24 <= len(segment_data):
        size = int.from_bytes(segment_data[pos : pos + 4], "big")
        if size <= 0 or pos + size > len(segment_data):
            break

        if bytes(segment_data[pos + 4 : pos + 8]) == b"uuid":
            return bytes(segment_data[pos + 8 : pos + 24])
        pos += size
    return None


def _decrypt_qobuz_segment(segment_data, raw_key, segment_uuid):
    if segment_uuid is None:
        return bytes(segment_data)

    buf = bytearray(segment_data)
    pos = 0
    while pos + 8 <= len(buf):
        size = int.from_bytes(buf[pos : pos + 4], "big")
        if size <= 0 or pos + size > len(buf):
            break

        if (
            bytes(buf[pos + 4 : pos + 8]) == b"uuid"
            and bytes(buf[pos + 8 : pos + 24]) == segment_uuid
        ):
            pointer = pos + 28
            data_end = pos + int.from_bytes(buf[pointer : pointer + 4], "big")
            pointer += 4
            counter_len = buf[pointer]
            pointer += 1
            frame_count = int.from_bytes(buf[pointer : pointer + 3], "big")
            pointer += 3

            for _ in range(frame_count):
                frame_len = int.from_bytes(buf[pointer : pointer + 4], "big")
                pointer += 6
                flags = int.from_bytes(buf[pointer : pointer + 2], "big")
                pointer += 2
                frame_start = data_end
                frame_end = frame_start + frame_len
                data_end = frame_end

                if flags:
                    counter = bytes(buf[pointer : pointer + counter_len]) + (
                        b"\x00" * (16 - counter_len)
                    )
                    decryptor = Cipher(
                        algorithms.AES(raw_key), modes.CTR(counter)
                    ).decryptor()
                    buf[frame_start:frame_end] = decryptor.update(
                        bytes(buf[frame_start:frame_end])
                    ) + decryptor.finalize()
                pointer += counter_len
        pos += size
    return bytes(buf)
```

`_download_and_tag` 里的重试循环改成“直连 5 次，再分段 5 次”（#306 的核心结构，节选）：

```python
        max_retries = 5
        last_error = None
        # Try with the normal mode first, then try using segments
        attempts = [
            {"force_segments": force_segments, "mode_retries": mode_retries}
            for force_segments in (False, True)
            for mode_retries in range(max_retries)
        ]
        for attempt in attempts:
            force_segments = attempt["force_segments"]
            mode_retries = attempt["mode_retries"]
            if mode_retries > 0:   # ← #306 原样；见下方“#306 自身的两个 bug”
                # 等待 2 ** mode_retries 秒，删掉残留的临时文件，然后：
                track_url_dict = self.client.get_track_url(
                    track_metadata["id"], fmt_id=self.quality,
                    force_segments=force_segments,
                )
            try:
                done = tqdm_download(track_url_dict, filename, filename,
                                     duration=track_metadata.get("duration"))
                if not done:
                    raise OSError(f"Download of {track_url_dict} failed")
                break
            except (requests.exceptions.ChunkedEncodingError,
                    requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout,
                    ConnectionError, OSError) as e:
                last_error = e
        else:
            # 两种方式都失败：删临时文件，抛出 last_error 或 DownloadError
            ...
```

> 完整版见 `refs/pr/306` 的 `qobuz_dl/downloader.py` 里的 `_download_and_tag`。注意 `tqdm_download`
> 现在接收的是**整个字典**，不再是 `url` 字符串。

**#306 自身的两个 bug（移植时顺手修掉）：**

1. **切换到分段模式的第一次尝试，其实还是直连。** 只有 `mode_retries > 0` 时才重新获取 URL，所以
   `force_segments=True, mode_retries=0` 这一次用的还是上一轮直连的 `track_url_dict`。分段模式实际上从第 2 次才开始，
   只有 4 次机会。修法：条件改成 `if mode_retries > 0 or force_segments:`（`mode_retries == 0` 时不用等待）。
2. **每次重试等了两遍。** 重新获取 URL 前 `time.sleep(wait)` 一次，获取后又 `time.sleep(wait)` 一次，
   5 次重试一共多等约 30 秒。删掉后面那次。

### 移植清单（结合本 fork 的现状）

按这个顺序做，每一项都对应合并时会遇到的一个坑：

1. **依赖**：加 `cryptography`；README 写明分段模式需要 ffmpeg。启动时可以用 `shutil.which("ffmpeg")` 检查，找不到就只警告。
2. **`qopy.py`**：照“代码 A”加入。登录逻辑**不要动**，#306 的 `auth()` 和本 fork 完全一样。
3. **`downloader.py` / `tqdm_download`**：照“代码 B”替换，然后：
   - 把 User-Agent 换成 `qopy.py` 里的 Chrome UA（#306 用的是 `config.USER_AGENT`，还是 Firefox 83）；
   - 分段请求的 `requests.head` / `requests.get` 都**补上 `timeout=(10, 60)`**，#306 漏了；
   - **限速**：`duration / playback_speed` 那段 sleep 会让下载速度不超过正常播放（60 分钟的专辑至少下 60 分钟），而且直连模式也会限速。建议改成参数，默认关闭，比如 `--throttle` 或者配置项 `playback_speed = 0` 表示不限速。
4. **重试循环**：用 #306 的“两种方式 × 5 次”替换本 fork 来自 #326 的 5 次重试，它是 #326 的超集。删掉 `download_release` 里来自 #326 的 `time.sleep(1)`（`get_track_url` 里已经有了）。
5. **失败时抛的异常**：#306 两种方式都失败后抛 `DownloadError`（要在 `exceptions.py` 里新增）。本 fork 的 `core.download_from_id` 只对 `RequestException` 做 5 分钟重试，需要决定：
   - `DownloadError` 也进 5 分钟重试，或者
   - 当作 `NonStreamable` 直接跳过。
6. **保留已合进来的修复**，#306 的代码会把它们冲掉：
   - #303：专辑文件夹名截断 `[:128]`，`makedirs` 出错时抛 `NonStreamable`；
   - #331：`_get_album_attr` 里的 `year` 要用 `(meta.get("release_date_original") or meta.get("release_date") or "")`，#306 又改回了会崩溃的 `meta["release_date_original"]`；
   - #308：文件名加 version，#306 也包含了，别加两次。
7. **`_get_format`**：#306 给它加了 `track_url_dict` 参数，免得为了判断格式再多请求一次 `get_track_url`。如果不移植这一项，每张专辑会多一次签名请求，在分段模式下还会多一次 `session/start`。
8. 移植后用真实账号分别测试：quality 5 / 6 / 7 / 27 各下一首；再强制走分段模式（临时把 `force_segments` 写死成 True）测一首 24-bit，确认 ffmpeg 转出来的 FLAC 能正常打标签和播放。

---

## 2. #306 的其他功能

都和下载方式无关，可以单独移植：

| 功能 | 实现要点 |
|---|---|
| `--dry-run` | 加在 `commands.py` 的 `add_common_arg`。`Download` 新增 `dry_run` 参数，在调用 `tqdm_download` 之前 `if self.dry_run: logger.info(...); return`；`core.download_from_id` 在 dry-run 时不写数据库 |
| `--verbose` | 同样走 `add_common_arg`。在 `QobuzDL.__init__` 里：`logging.getLogger("qobuz_dl").setLevel(logging.DEBUG)`。各模块已经有很多 `logger.debug` 可以用 |
| 配置项 `dry_run` / `verbose` | **注意**：#306 用 `config.getboolean("DEFAULT", "dry_run")` 读取，没有默认值，旧配置会被判定为“损坏”。移植时要写成 `config.getboolean("DEFAULT", "dry_run", fallback=False)` |
| 日志加时间戳 | `cli.py`：`logging.basicConfig(format='%(asctime)s %(levelname)-8s %(message)s', datefmt='%Y-%m-%d %H:%M:%S')` |
| 跳过不可播放的曲目 | 专辑循环里：`if not i.get("streamable"): logger.info(...); count += 1; continue` |
| 汇总失败的曲目 | 专辑循环里对每首 `try/except` 收集到 `last_errors`，最后统一打印；有失败时抛 `DownloadError`，这样 `core` **就不会把整张专辑记进数据库**，下次运行会自动补下 |
| 每张碟显示进度 | `media_count = len({*media_numbers})`，换碟时打印 `Media N of M` |
| 保存元数据 | `Client.trace_meta(directory, item_type, item_id, item_meta)`：把 API 返回的 JSON 存到 `<下载目录>/.qobuz/<类型>/<id>.json` |
| `config.py` | 把 `CONFIG_PATH`、`CONFIG_FILE`、`QOBUZ_DB`、`USER_AGENT` 和各种提示文字集中到一个文件里。本 fork 的 `cli.py` 和 `qopy.py` 现在各算一遍配置路径，可以借这个思路合并 |

**不要移植**：#306 删掉了 `--show-config`（`-sc`）；常量 `DEFAULT_FOLDER` / `DEFAULT_TRACK` 改名成 `*_FORMAT`。这两处改动都是破坏性的，没有必要。

---

## 3. #334 的加固项

#334 的核心思路是“直接拿 `user_id` + token 登录、不续期”，和本 fork“每次刷新 UAT”的方向不同，**不整个合**。可以单独挑这些：

### 3.1 所有请求加超时

```python
# Network timeouts: (connect_seconds, read_seconds).
_REQUEST_TIMEOUT = (5, 30)
# qopy.py 里每个 self.session.get/post 都传 timeout=_REQUEST_TIMEOUT
```

### 3.2 报错时不泄露 token

`r.raise_for_status()` 抛出的 `HTTPError` 会带上完整 URL。`favorite/getUserFavorites` 把 `user_auth_token` 放在 query 参数里，所以 token 会出现在报错和日志里。#334 的写法：

```python
        if not r.ok:
            raise requests.HTTPError(f"HTTP {r.status_code} from {epoint}") from None
```

400 → `InvalidAppSecretError` 时也只取服务器返回的 `message` 字段，不打印整个 JSON。

### 3.3 安全写配置（`qobuz_dl/_config_io.py`）

```python
import contextlib
import os
import tempfile


def atomic_write_config(path, parser):
    target_dir = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(target_dir, exist_ok=True)

    tmp = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=target_dir,
        prefix=".config-", suffix=".tmp", delete=False,
    )
    tmp_name = tmp.name
    try:
        parser.write(tmp)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.close()
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise

    if os.name != "nt":
        os.chmod(path, 0o600)
        try:
            dirfd = os.open(target_dir, os.O_RDONLY)
        except OSError:
            return
        try:
            with contextlib.suppress(OSError):
                os.fsync(dirfd)
        finally:
            os.close(dirfd)
```

本 fork 每次运行都会把刷新后的 token 写回 `config.ini`（`qopy.Client.auth`），写到一半被中断，配置就坏了，所以这一项**比 #334 原本的场景更有用**。`cli._reset_config` 也可以改用它。

### 3.4 `--show-config` 隐藏密钥

- 默认把 `user_auth_token`、`password`、`secrets` 的值替换成 `***REDACTED***`，加 `-S/--show-secrets` 才显示原文。
- 用 `configparser` 读一遍再写出来（多行的值也能完整隐藏）；解析失败时退回到逐行正则替换。
- 配置损坏时也要能用（`--show-config`、`-r`、`-p` 要在读取配置**之前**处理）。

### 3.5 其他小项

- **会员名称**：按 `credential.parameters.short_label` → `parameters.label` → `credential.label` 的顺序找，都没有就显示 `"Unknown"`。本 fork 现在直接取下标，返回格式一变就会 `KeyError`。
- **免费账号**：`parameters` 为空时抛 `IneligibleError`。本 fork 的 `auth()` 现在不检查，会在后面报一个看不懂的错误。
- **找不到可用的 secret 时降级**：`cfg_setup()` 失败时只警告，登录和搜索照常；`dl` / `fun` / `lucky` 以退出码 3 退出并说明原因，不打印 traceback。
- **`logging.basicConfig` 挪进 `main()`**：这样把 qobuz_dl 当库导入时，不会改掉调用方的日志设置。
- **测试**：`tests/` 下的 pytest 结构（fixtures 里有各种 `user/login` 返回样例）可以参考，但测的是 #334 自己的接口，不能直接用。

---

## 4. OAuth 登录（#331）

在浏览器里登录，本地起一个 HTTP 服务器接收授权码，不用再手动复制 token。

**和本 fork 很契合**：#331 拿到 OAuth token 之后，第二步就是 `POST user/login`、`extra=partner`、请求头带 `X-User-Auth-Token`，**和本 fork 的 `auth()` 一模一样**。所以只要把 OAuth 拿到的 token 存进 `password` 字段，后面的流程完全不用改。

流程：

```
1. bundle 里用正则 privateKey:\s*"(?P<key>[A-Za-z0-9]{6,30})" 提取 private_key，存进 config
2. 在本地找一个空闲端口，打开
   https://www.qobuz.com/signin/oauth?ext_app_id=<app_id>&redirect_url=http://localhost:<port>
3. 本地 HTTPServer 处理一次回调，从 query 里取 code（或 code_autorisation）
4. GET oauth/callback?code=<code>&private_key=<private_key>&app_id=<app_id>  → 返回 {"token": ...}
5. 拿这个 token 走 user/login extra=partner（= 本 fork 的 Client.auth）
```

`login_with_oauth_code`（出自 #331）：

```python
    def login_with_oauth_code(self, code, private_key):
        r = self.session.get(self.base + "oauth/callback", params={
            "code": code, "private_key": private_key, "app_id": self.id,
        })
        r.raise_for_status()
        token = r.json().get("token")
        if not token:
            raise AuthenticationError("No token in OAuth callback response")
        # 之后等同于本 fork 的 self.auth(None, token)
```

移植要点：

- 新增子命令 `qobuz-dl oauth [code]`。不带参数时起本地服务器；带参数时接受授权码或完整的回调 URL。
- #331 的本地服务器只调用一次 `handle_request()`，再用 `input()` 等用户按回车，用起来比较别扭。可以改成：用 `webbrowser.open()` 自动打开浏览器，`server.timeout` 设成 120 秒，收到回调就自动继续。
- #331 先绑定端口 0 拿到空闲端口，关掉后再重新绑定，中间有一小段时间端口可能被别的程序占用。直接 `HTTPServer(("127.0.0.1", 0), ...)`，然后读 `server.server_port` 就行。
- **风险**：`private_key` 正则和 `oauth/callback` 端点都是从网页播放器逆向出来的，PR 里也没有说明在多少账号上测过。移植后先用自己的账号验证。

---

## 5. #326 的登录部分

不建议用，记下来仅供对照：

- 登录改成 `GET user/get`（请求头带 token）验证。返回的是**扁平结构**：`credential` 在顶层，不在 `user` 下面。如果返回里有新的 `user_auth_token`，就写回配置。
- `password` 长度小于 60 时，当作真实密码，先用表单 `POST user/login`（email + password）换 token。Qobuz 已经在服务端关闭了邮箱密码登录，这条路基本走不通了。
- 如果哪天 `user/login extra=partner` 不能用了，`user/get` 可以作为验证 token 的备用端点。

## 6. #282

2024 年的 token 登录方案：`Client(..., token=...)`，带 token 请求 `user/login`。已经被本 fork 的方案和 #334 取代，**不用看**。

---

## 附录：上游 PR 在本 fork 中的状态

| PR | 状态 |
|---|---|
| #185 `me` 命令 | ✅ 已合（修了翻页 bug） |
| #235 m3u 用 UTF-8 | ✅ 已合 |
| #236 超过 500 项翻页 | ✅ 已合 |
| #302 last.fm 翻页 | ✅ 已合 |
| #303 文件夹名截断 | ✅ 已合 |
| #307 Chrome UA | ✅ 已合 |
| #308 文件名加 version | ✅ 已合 |
| #326 | 🟡 只挑了 `downloader.py` 的重试；登录部分见第 5 节 |
| #331 | 🟡 只挑了两个日期修复；OAuth 见第 4 节 |
| #306 | 📄 见第 1、2 节 |
| #334 | 📄 见第 3 节；localStorage 拿 token 的方法已经用在 `qopy.TOKEN_HOWTO` 里 |
| #282 | ❌ 已过时 |
