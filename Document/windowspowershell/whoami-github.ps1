# 檢查目前資料夾 GitHub 遠端設定
Write-Host "🔍 檢查 GitHub 遠端設定..."
$remote = git remote -v 2>$null
if ($remote) {
    $repoUrl = ($remote | Select-String -Pattern "origin").ToString()
    Write-Host "🌐 Remote URL: $repoUrl"
} else {
    Write-Host "⚠️ 沒有設定遠端 GitHub Repo"
}

# 檢查 GitHub CLI 登入帳號
Write-Host "`n🔍 檢查 GitHub CLI 登入..."
try {
    $ghStatus = gh auth status 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Host $ghStatus
    } else {
        Write-Host "⚠️ 沒有透過 GitHub CLI 登入"
    }
} catch {
    Write-Host "⚠️ 沒有安裝 GitHub CLI"
}

# 檢查 Git commit 使用者資訊
Write-Host "`n🔍 Git commit 設定："
$userName = git config --global user.name
$userEmail = git config --global user.email
if ($userName -and $userEmail) {
    Write-Host "👤 user.name : $userName"
    Write-Host "📧 user.email: $userEmail"
} else {
    Write-Host "⚠️ 尚未設定全域 Git 使用者資訊"
}

