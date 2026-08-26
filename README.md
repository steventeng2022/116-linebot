# 116 LINE Bot

音控團隊使用的 LINE Bot，提供成員身分登記、活動出席回覆及管理員網頁。

## 功能

- 私訊輸入 `身分驗證`，依序填寫姓名、班級與座號
- 管理員會收到 LINE 推播通知
- `https://bot.steventeng.uk/admin` 查看與匯出驗證資料
- `/event 活動名稱` 建立活動，成員可選擇有空／沒空
- `/list` 查看最新活動名單

## 本機測試

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python smoke_test.py
```

環境變數請參考 `.env.example`。不要提交真正的 LINE Token、密碼或資料庫。

## Cloudflare 網域代理

`worker.js` 只負責將 `bot.steventeng.uk` 的流量串流轉送到 Linode HTTPS 來源；應用程式與學生資料不儲存在 Cloudflare。`wrangler.jsonc` 讓 Cloudflare GitHub Builds 可以驗證並部署這個代理層。

## 管理員綁定

伺服器設定 `ADMIN_SETUP_TOKEN` 後，由管理員私訊 Bot：

```text
/設定管理員 一次性設定代碼
```

綁定後，新的身分資料會透過 LINE 推播給管理員。
設定代碼只允許第一次成功綁定，使用後會立即失效。
