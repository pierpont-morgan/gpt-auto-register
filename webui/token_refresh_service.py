import threading
import time
import logging
from typing import Optional, List, Dict, Any

from batch_refresh import refresh_one_account, push_to_chatgpt2api, update_db_account
from webui import db
import sqlite3
import os

logger = logging.getLogger("token_refresh_manager")

class TokenRefreshManager:
    def __init__(self):
        self._lock = threading.Lock()
        self.is_running = False
        self.total = 0
        self.current = 0
        self.success_count = 0
        self.fail_count = 0
        self.current_email = ""
        self.logs: List[str] = []
        self._stop_requested = False

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "is_running": self.is_running,
                "total": self.total,
                "current": self.current,
                "success_count": self.success_count,
                "fail_count": self.fail_count,
                "current_email": self.current_email,
                "logs": self.logs[-20:], # 最近 20 条日志
            }

    def stop(self):
        with self._lock:
            self._stop_requested = True
            self.logs.append("[用户操作] 已请求停止任务...")

    def start_refresh(self, emails: Optional[List[str]] = None, only_expired: bool = False):
        with self._lock:
            if self.is_running:
                return False, "已有刷新任务正在执行中"
            self.is_running = True
            self._stop_requested = False
            self.current = 0
            self.success_count = 0
            self.fail_count = 0
            self.logs = []
            self.logs.append("开始准备刷新任务...")

        thread = threading.Thread(target=self._worker, args=(emails, only_expired), daemon=True)
        thread.start()
        return True, "任务已启动"

    def _worker(self, emails: Optional[List[str]], only_expired: bool):
        try:
            db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "webui", "webui.db")
            conn = sqlite3.connect(db_path)
            c = conn.cursor()

            if emails and len(emails) > 0:
                placeholders = ",".join("?" * len(emails))
                rows = c.execute(
                    f"SELECT email, password, totp_secret FROM registered "
                    f"WHERE email IN ({placeholders}) "
                    f"AND totp_secret IS NOT NULL AND totp_secret != '' "
                    f"AND password IS NOT NULL AND password != ''",
                    emails
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT email, password, totp_secret, access_token FROM registered "
                    "WHERE totp_secret IS NOT NULL AND totp_secret != '' "
                    "AND password IS NOT NULL AND password != ''"
                ).fetchall()
                if only_expired:
                    now = time.time()
                    filtered = []
                    for r in rows:
                        email_addr, pwd, secret, token = r
                        exp = 0
                        if token and "." in token:
                            try:
                                import base64, json
                                parts = token.split(".")
                                if len(parts) >= 2:
                                    p = parts[1]
                                    pad = (4 - len(p) % 4) % 4
                                    payload = json.loads(base64.urlsafe_b64decode(p + "=" * pad))
                                    exp = int(payload.get("exp") or 0)
                            except Exception:
                                pass
                        # 仅选取没有 Token 或 Token 已过期的账号
                        if exp <= now:
                            filtered.append((email_addr, pwd, secret))
                    rows = filtered
                else:
                    rows = [(r[0], r[1], r[2]) for r in rows]

            conn.close()

            with self._lock:
                self.total = len(rows)
                self.logs.append(f"共扫描出 {self.total} 个待刷新账号")

            for idx, (email_addr, password, totp_secret) in enumerate(rows, 1):
                with self._lock:
                    if self._stop_requested:
                        self.logs.append("任务已被用户提前终止")
                        break
                    self.current = idx
                    self.current_email = email_addr
                    self.logs.append(f"[{idx}/{self.total}] 开始刷新: {email_addr}")

                try:
                    result = refresh_one_account(email_addr, password, totp_secret)
                    if result and result.get("access_token"):
                        update_db_account(email_addr, result["access_token"], result.get("session_token", ""))
                        push_to_chatgpt2api(email_addr, result["access_token"], result.get("refresh_token", ""))
                        with self._lock:
                            self.success_count += 1
                            self.logs.append(f"✅ [{email_addr}] 刷新成功，已同步至 ChatGPT2API")
                    else:
                        with self._lock:
                            self.fail_count += 1
                            self.logs.append(f"❌ [{email_addr}] 刷新失败")
                except Exception as e:
                    with self._lock:
                        self.fail_count += 1
                        self.logs.append(f"❌ [{email_addr}] 异常: {str(e)[:100]}")

                time.sleep(2)

            with self._lock:
                self.logs.append(f"任务结束！成功: {self.success_count}，失败: {self.fail_count}")

        except Exception as e:
            with self._lock:
                self.logs.append(f"执行异常退出: {e}")
        finally:
            with self._lock:
                self.is_running = False
                self.current_email = ""

refresh_manager = TokenRefreshManager()
