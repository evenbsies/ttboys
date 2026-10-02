# 部署到 GitHub・操作清单

目标：把 `tvbox-daily` 变成 GitHub 仓库，每天自动抓取源并生成 `merged.json`，你订阅它的链接即可。

本清单零依赖（不需要安装 git，全程网页操作）。



***

## 第 1 步：注册 / 登录 GitHub

打开 [https://github.com](https://github.com) → 注册（需要邮箱）或登录。

没有账号的话，注册时用户名（Username）就是以后订阅链接里的 "你的用户名"，记好它。

## 第 2 步：新建仓库



1. 点右上角 **+** → **New repository**

2. Repository name 填：`tvbox-daily`（可以改，但记住用了什么）

3. **Public**（必须公开，jsDelivr 加速只服务公开仓库；Actions 免费额度公开仓库不限量）

4. 其他默认，点 **Create repository**

## 第 3 步：上传文件（分两批）

### 第一批：根目录的 7 个文件

仓库主页点 **Add file** → **Upload files**，把 `tvbox-daily` 文件夹里这 7 个文件**直接拖进网页**：

`fetch_sources.py`、`sources.txt`、`blocklist.txt`、`merged.json`、`status.json`、`README.md`、`反赌博广告手册.md`

拖入后点绿色 **Commit changes**。

### 第二批：定时任务文件（要建子目录）

网页上传无法直接选子目录文件，用 "新建文件" 方式创建：



1. 点 **Add file** → **Create new file**

2. 在文件名输入框里**完整输入**：`.github/workflows/daily.yml`

   （GitHub 会自动创建 `.github/workflows` 两级目录）

3. 打开你本地的 `tvbox-daily\.github\workflows\daily.yml`，全选复制内容，粘贴进网页编辑器

4. 点绿色 **Commit changes**

完成后仓库根目录应能看到：7 个文件 + `.github` 文件夹（点进去应有 `workflows/daily.yml`）。

## 第 4 步：手动触发一次（验证）



1. 仓库顶部切到 **Actions** 标签

2. 左侧点 **daily-fetch** 工作流

3. 右侧点 **Run workflow** → 绿色按钮确认

4. 等 1\~3 分钟，看到运行记录变绿 ✓ 即成功

> 之后它会每天 UTC 22:00（北京时间次日 06:00）自动跑，无需再手动。

## 第 5 步：验证产物已更新

Actions 跑完后回到仓库首页 → 点 `merged.json` → 看内容里的 `update_time` 是不是刚才的时间；或看仓库最近的 Commit 记录里有没有 "daily update 2026-10-02" 这类提交。

## 第 6 步：拿到订阅链接（二选一）



* **官方 raw**（海外网络 / 翻墙环境用）：

  `https://raw.githubusercontent.com/你的用户名/tvbox-daily/main/merged.json`

* **国内加速 jsDelivr**（国内直连推荐）：

  `https://cdn.jsdelivr.net/gh/你的用户名/tvbox-daily@main/merged.json`

## 第 7 步：填进播放器

TVBox / 影视仓 / OK 影视 → 设置 → 配置地址 → 粘贴上面的链接 → 确定 / 导入。

能打开首页列出片源即部署成功。



***

## 日常维护



| 想做什么    | 怎么做                                                                     |
| ------- | ----------------------------------------------------------------------- |
| 加 / 删源  | 本地改 `sources.txt` → 网页 Add file→Upload files 重新上传覆盖该文件（Actions 每天会用新种子） |
| 加广告黑名单  | 改 `blocklist.txt` 重新上传覆盖                                                |
| 立即跑一次   | Actions → daily-fetch → Run workflow                                    |
| 看运行失败原因 | Actions → 点某次运行 → 展开失败步骤看日志                                             |
| 更新爬虫脚本  | 改 `fetch_sources.py` 后重新上传覆盖；改完建议手动触发一次验证                               |

## 常见问题



* **Actions 页面是灰的 / Run workflow 点不了**：仓库是新建的，等几秒刷新；仍不行检查仓库 Settings → Actions → General 是否被禁用（默认启用）。

* **订阅链接打不开**：确认用户名 / 仓库名拼写一致；确认仓库是 Public；国内打不开 raw 就用 jsDelivr 链接。

* **每天没自动跑**：schedule 从 workflow 入库后的下一个整点才生效，首次请先手动触发一次；GitHub 的定时任务会有 0\~15 分钟随机延迟，属正常。

* **jsDelivr 缓存**：改文件后可能要等几分钟才更新，或手动访问 `https://cdn.jsdelivr.net/gh/你的用户名/tvbox-daily@main/merged.json` 刷新缓存。

* **公开仓库安全**：仓库里不要放任何账号、Token、网盘凭证；本项目的三个配置文件均为公开源地址，无敏感信息。