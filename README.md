# 获客雷达 · AI 评论区获客工具

盯住同行**抖音 / 小红书 / 快手**的评论区，AI 自动识别「咨询 / 想买」的评论、拟好个性化私信，你点一下发送。

## 功能

- 监控同行博主主页最新视频的评论区（抖音 / 小红书 / 快手）
- DeepSeek AI 批量判断评论意图（咨询 / 购买 / 闲聊 / 负面），并草拟私信
- 一键半自动发私信：点「发送」弹出浏览器自动操作，发完自动标记
- 内置每日额度 + 各平台扫描节奏，防封号
- 数据全部存在本机 SQLite，不上传任何服务器

## 新电脑安装（Windows）

1. 装 **Python 3.12**（到 python.org 下载，安装时勾选 ✅ *Add Python to PATH*）。
2. 下载本项目：
   - 方法 A：GitHub 仓库页面点 **Code → Download ZIP**，解压到任意文件夹；
   - 方法 B：命令行 `git clone https://github.com/729865273lq-gif/huoke-radar.git`
3. 进入项目文件夹，把 `.env.example` **复制一份、改名为 `.env`**，填上你的 DeepSeek API Key：
   ```
   DEEPSEEK_API_KEY=sk-你的key
   ```
   （Key 在 DeepSeek 开放平台 platform.deepseek.com 获取；也可从旧电脑的 `.env` 里复制。）
4. 双击 `run.bat`。
   - 第一次运行会自动装依赖、下载浏览器内核（约几分钟，**只需一次**）。
   - 完成后浏览器自动打开 `http://127.0.0.1:8080`。
5. 进页面「发信账号」区扫码登录抖音 / 小红书，然后按 `使用指南.md` 操作。

## 手动安装（run.bat 没生效时）

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\playwright install chromium
.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8080
```

## 多开实例

双击 `多开.bat`，一个行业 / 一个客户开一个独立后台（独立数据库、独立登录）。详见 `使用指南.md` 第六节。

## 重要提醒

- **数据不迁移**：每台电脑是独立后台，新电脑从零开始；旧电脑数据仍在旧电脑上。
- **账号别两台电脑同时登录同一个号**：抖音风控会判定异常，建议两台电脑各用各的号，或错开使用时间。
- 本工具只用于监控公开评论区、向主动咨询的潜在客户发私信。请遵守平台规则：控制发送量、只发一条、不轰炸。平台可能因引流行为限制账号。
