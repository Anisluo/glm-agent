"""infra.jsonio —— JSON 读写与后台保存线程。"""
import json
import logging
import os
import shutil
import threading
import time

from infra.config import LOAD_PROBLEMS

log = logging.getLogger("glm")


def _read_json(path):
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def load_json(path, default):
    """读取 JSON。文件损坏时先把它备份，再尝试用上一版 .bak 恢复，绝不让坏文件被空数据覆盖。"""
    try:
        return _read_json(path)
    except FileNotFoundError:
        return default
    except (ValueError, OSError) as e:
        bad = f"{os.path.splitext(path)[0]}.broken-{time.strftime('%Y%m%d-%H%M%S')}.json"
        try:
            shutil.copy2(path, bad)
        except OSError:
            bad = ""
        log.error("读取 %s 失败：%s；已备份为 %s", path, e, bad)
        msg = f"{os.path.basename(path)} 已损坏无法读取" + (f"，原文件已备份为 {os.path.basename(bad)}" if bad else "")
        try:
            data = _read_json(path + ".bak")
            LOAD_PROBLEMS.append(msg + "，已从上一次保存的备份恢复。")
            return data
        except (ValueError, OSError):
            LOAD_PROBLEMS.append(msg + "，也没有可用的备份。")
            return default


def _write_text_atomic(path, text):
    """先写临时文件再替换，替换前把上一版留作 .bak；文件被占用时自动重试。成功返回 True。"""
    tmp = path + ".tmp"
    for attempt in range(3):
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            if os.path.exists(path):
                shutil.copy2(path, path + ".bak")
            os.replace(tmp, path)
            return True
        except OSError as e:
            log.warning("保存 %s 失败（第 %d 次）：%s", path, attempt + 1, e)
            time.sleep(0.3)
    return False


def save_json(path, data):
    """同步保存（用于配置、记忆等体积小、不频繁的数据）。"""
    return _write_text_atomic(path, json.dumps(data, ensure_ascii=False, indent=2))


class _AsyncSaver(threading.Thread):
    """后台写盘线程：合并同一文件的多次保存，只写最新内容，避免阻塞界面。"""

    def __init__(self):
        super().__init__(name="saver", daemon=True)
        self._latest = {}            # path -> 待写入的最新文本
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = False

    def submit(self, path, text):
        with self._lock:
            self._latest[path] = text
        self._wake.set()

    def run(self):
        while not self._stop:
            self._wake.wait()
            self._wake.clear()
            self._drain()

    def _drain(self):
        while True:
            with self._lock:
                if not self._latest:
                    return
                path, text = self._latest.popitem()
            _write_text_atomic(path, text)

    def flush(self):
        """退出前把剩余内容同步写完。"""
        self._stop = True
        self._wake.set()
        self._drain()


SAVER = _AsyncSaver()
SAVER.start()
