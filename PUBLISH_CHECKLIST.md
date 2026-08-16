# 第一次发布到 GitHub

这份清单假设你已经有 GitHub 账号，但还没有使用过 GitHub。公开版暂存目录在本机的实际位置由你决定；文中的 `<本地公开版目录>` 代表它。

```text
<本地公开版目录>
```

这个路径只是在当前电脑上的准备目录，不会自动公开，也没有绑定任何远程仓库。

## 发布前检查

1. 打开根目录 `README.md`，确认你愿意公开当前功能边界和 GPL-3.0-only 许可证。
2. 确认 `onebot-plugin/config.example.json` 仍然只有占位符；真实配置不要复制进来。
3. 确认 NapCat 端和 OneBot 端来自同一份公开版目录，不要只上传其中一端。
4. 确认没有把 QQ、NapCat、AVSDK、模型权重、声纹、录音、日志或令牌复制进来。

## 用网页创建空仓库

1. 登录 <https://github.com>，点击右上角头像旁的 `+`，选择 `New repository`。
2. 仓库名可以先用 `qq-voice-call-bridge`；如果这个名字已被占用，换一个相近的名字即可。
3. `Visibility` 选择 `Public`。
4. 不要勾选初始化 README、`.gitignore` 或 License。公开版目录已经包含这些文件。
5. 点击 `Create repository`，复制 GitHub 显示的 HTTPS 仓库地址。不要把这个地址写进公开文档模板。

## 在本机建立第一次提交

在 PowerShell 中执行下面的命令。每一步都可以单独执行，看到错误时停下来，不要重复推送：

本次整理已经在暂存目录初始化了本地 Git，但还没有创建提交或远程地址。如果你使用的就是这个目录，可以从 `git add .` 开始；如果你把文件复制到了其他目录，再先运行 `git init`。

```powershell
cd <本地公开版目录>
git add .
git status
git commit -m "Initial public release"
git branch -M main
git remote add origin <你刚刚复制的仓库地址>
git push -u origin main
```

第一次 `git commit` 如果提示没有设置作者信息，可以只在这个仓库设置：

```powershell
git config user.name "你的 GitHub 显示名"
git config user.email "你的 GitHub 公开邮箱或 noreply 邮箱"
```

如果你不想在公开提交里显示真实邮箱，可以在 GitHub 账号设置的邮箱页面启用 `noreply` 地址，再把上面的邮箱换成它。不要把密码、Cookie 或访问令牌写进命令、文件或 issue。

## 后续更新

以后修改文件后，在同一个目录执行：

```powershell
git add .
git status
git commit -m "Describe the change"
git push
```

GitHub 页面上的 `Code` 按钮可以复制仓库地址，`Releases` 可以在确认版本稳定后发布带说明的版本；第一次发布不需要马上制作 Release。

## 上传失败时

- 如果 `git` 命令不存在，先安装 Git for Windows，或改用 GitHub Desktop；不要把整个私有项目根目录拖到 GitHub 网页。
- 如果 `git push` 要求登录，使用 GitHub 官方登录流程、Git Credential Manager 或 GitHub Desktop，不要把 GitHub 密码当作 Git 密码输入。
- 如果 GitHub 页面显示仓库里有不应公开的文件，立即停止后删除远程仓库中的公开提交，再回到本地检查 `.gitignore` 和 `git status`；不要继续追加覆盖提交。
