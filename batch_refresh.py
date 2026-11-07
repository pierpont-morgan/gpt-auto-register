import os
import sys
import time
import re
import imaplib
import email
import sqlite3
import logging
from typing import Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

# 全局关闭容易打断主会话的 Codex RT 抢跑逻辑
os.environ["OAUTH_CODEX_RT_BEFORE_CALLBACK"] = "0"
os.environ["OAUTH_CODEX_RT_EXCHANGE"] = "0"

from config import Config
from auth_flow import AuthFlow
from mail_providers.base import MailProvider
import curl_cffi.requests as requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("batch_refresh")

# 尝试读取本地 .env 文件
env_file = os.path.join(BASE_DIR, ".env")
if os.path.exists(env_file):
    with open(env_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))

DB_PATH = os.path.join(BASE_DIR, "webui", "webui.db")
CHATGPT2API_URL = os.environ.get("CHATGPT2API_URL", "http://127.0.0.1:3000/api/accounts")
CHATGPT2API_KEY = os.environ.get("CHATGPT2API_KEY", "chatgpt2api")
PROXY_URL = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or "http://127.0.0.1:7897"

# iCloud 母邮箱凭证（从环境变量或 .env 读取）
ICLOUD_MOTHER_EMAIL = os.environ.get("ICLOUD_MOTHER_EMAIL", "")
ICLOUD_APP_PASSWORD = os.environ.get("ICLOUD_APP_PASSWORD", "")
IMAP_HOST = os.environ.get("IMAP_HOST", "imap.mail.me.com")
IMAP_PORT = int(os.environ.get("IMAP_PORT", "993"))

class DirectICloudIMAPMailProvider(MailProvider):
    """直接直连 Apple 官方 IMAP 服务器自动接码，无需 4173 中转"""
    def __init__(self, target_email: str):
        self.target_email = target_email.lower().strip()

    def create_mailbox(self) -> str:
        return self.target_email

    def fetch_otp(self, mailbox: str, timeout: int = 120) -> str:
        return self.wait_for_otp(mailbox, timeout)

    def wait_for_otp(self, mailbox: str, timeout: int = 120, issued_after: Optional[float] = None) -> str:
        # 允许 15 秒的时间漂移缓冲
        cutoff = (issued_after - 15) if issued_after else (time.time() - 30)
        start_time = time.time()
        logger.info(f"[DirectIMAP] 正在直连 Apple IMAP 监听发往 {self.target_email} 的最新验证码 (cutoff={cutoff})...")

        seen_codes = set()

        while time.time() - start_time < timeout:
            try:
                imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=15)
                imap.login(ICLOUD_MOTHER_EMAIL, ICLOUD_APP_PASSWORD)
                imap.select("INBOX")
                typ, data = imap.search(None, "ALL")
                if typ == "OK" and data and data[0]:
                    msg_ids = data[0].split()
                    # 倒序检查最新的 5 封邮件
                    for msg_id in reversed(msg_ids[-5:]):
                        typ, msg_data = imap.fetch(msg_id, "(RFC822)")
                        if typ != "OK" or not msg_data or not msg_data[0]:
                            continue
                        msg = email.message_from_bytes(msg_data[0][1])
                        
                        date_str = str(msg.get("Date", ""))
                        try:
                            msg_timestamp = email.utils.parsedate_to_datetime(date_str).timestamp()
                        except Exception:
                            msg_timestamp = 0
                            
                        if msg_timestamp < cutoff:
                            continue

                        to_addr = str(msg.get("To", "")).lower()
                        subject = str(msg.get("Subject", ""))
                        
                        if self.target_email in to_addr or "chatgpt" in subject.lower() or "openai" in subject.lower():
                            body_bytes = b""
                            if msg.is_multipart():
                                for part in msg.walk():
                                    if part.get_content_type() in ("text/plain", "text/html"):
                                        body_bytes += part.get_payload(decode=True) or b""
                            else:
                                body_bytes = msg.get_payload(decode=True) or b""
                            
                            text = body_bytes.decode("utf-8", errors="ignore")
                            # 提取 6 位验证码：优先匹配提示语后的，或者单独大字号区块里的
                            # 排除像 style="color:#202123" 这种十六进制色值干扰
                            m_target = re.search(r'(?:code to continue|verification code|验证码)[^\d]{0,50}([0-9]{6})', text, re.I)
                            if m_target:
                                code = m_target.group(1)
                            else:
                                # 匹配两端不是 # 或其他字母的纯 6 位数字
                                candidates = re.findall(r'(?<![#0-9A-Fa-f])([0-9]{6})(?![0-9A-Fa-f])', text)
                                code = candidates[0] if candidates else None

                            if code:
                                logger.info(f"🎉 [DirectIMAP] 捕获到匹配时间的新验证码: {code} (邮件时间: {date_str})")
                                imap.close()
                                imap.logout()
                                return code
                imap.close()
                imap.logout()
            except Exception as e:
                logger.warning(f"[DirectIMAP] 轮询异常 (重试中): {e}")

            time.sleep(3)

        raise TimeoutError(f"等待 {self.target_email} 验证码超时 ({timeout}s)")

def push_to_chatgpt2api(email: str, access_token: str, refresh_token: str = "") -> bool:
    headers = {
        "Authorization": f"Bearer {CHATGPT2API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "accounts": [
            {
                "email": email,
                "access_token": access_token,
                "refresh_token": refresh_token
            }
        ]
    }
    try:
        r = requests.post(CHATGPT2API_URL, headers=headers, json=payload, timeout=15)
        if r.status_code == 200:
            return True
        logger.warning(f"[{email}] 推送失败 HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        logger.warning(f"[{email}] 推送异常: {e}")
    return False

def update_db_account(email: str, access_token: str, session_token: str = ""):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            "UPDATE registered SET access_token = ?, session_token = ? WHERE email = ?",
            (access_token, session_token, email)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"[{email}] 更新本地数据库异常: {e}")

def refresh_one_account(email: str, password: str, totp_secret: str) -> Optional[dict]:
    cfg = Config(proxy=PROXY_URL)
    
    def account_cred_callback(em: str):
        return {"password": password, "totp_secret": totp_secret}

    flow = AuthFlow(
        config=cfg,
        account_callback=account_cred_callback
    )
    flow.result.totp_secret = totp_secret

    mail_provider = DirectICloudIMAPMailProvider(email)
    try:
        res = flow.run_protocol_login(mail_provider, email=email, password=password)
        if res.access_token:
            return {
                "email": email,
                "access_token": res.access_token,
                "session_token": res.session_token,
                "refresh_token": getattr(res, "refresh_token", "")
            }
        else:
            logger.warning(f"[{email}] 登录完成但未获取到 access_token")
    except Exception as e:
        logger.error(f"[{email}] 登录失败: {e}")
    return None

def main():
    target_arg = sys.argv[1] if len(sys.argv) > 1 else None

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    if target_arg and target_arg.isdigit():
        # 如果传入的是数字，例如 python batch_refresh.py 5，表示只跑前 N 个
        limit_num = int(target_arg)
        rows = c.execute(
            "SELECT email, password, totp_secret FROM registered "
            "WHERE totp_secret IS NOT NULL AND totp_secret != '' "
            "AND password IS NOT NULL AND password != '' "
            "LIMIT ?",
            (limit_num,)
        ).fetchall()
    elif target_arg:
        # 单个指定邮箱
        rows = c.execute(
            "SELECT email, password, totp_secret FROM registered WHERE email = ?",
            (target_arg,)
        ).fetchall()
    else:
        # 仅刷新已完成注册并配置好 2FA 的账号（动态判断 totp_secret 和 password）
        rows = c.execute(
            "SELECT email, password, totp_secret FROM registered "
            "WHERE totp_secret IS NOT NULL AND totp_secret != '' "
            "AND password IS NOT NULL AND password != ''"
        ).fetchall()
    conn.close()

    total = len(rows)
    logger.info(f"==> 共找到 {total} 个待刷新账号")

    success_count = 0
    fail_count = 0

    for idx, (email, password, totp_secret) in enumerate(rows, 1):
        logger.info(f"\n[{idx}/{total}] 正在处理: {email}")
        result = refresh_one_account(email, password, totp_secret)
        if result and result.get("access_token"):
            update_db_account(email, result["access_token"], result.get("session_token", ""))
            pushed = push_to_chatgpt2api(email, result["access_token"], result.get("refresh_token", ""))
            if pushed:
                logger.info(f"✅ [{email}] 刷新成功并已自动打入 ChatGPT2API 号池！")
            else:
                logger.info(f"⚠️ [{email}] 刷新成功，但推送到 ChatGPT2API 失败")
            success_count += 1
        else:
            logger.error(f"❌ [{email}] 刷新失败")
            fail_count += 1
        
        time.sleep(2)

    logger.info(f"\n================ 总结 ================")
    logger.info(f"总计: {total} | 成功: {success_count} | 失败: {fail_count}")

if __name__ == "__main__":
    main()
