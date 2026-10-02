# Push all files in tvbox-daily to GitHub repo via Contents API (no git needed)
$gh = "C:\Users\Administrator\gh-cli\bin\gh.exe"
$env:HTTPS_PROXY = 'http://127.0.0.1:7897'
$env:HTTP_PROXY = 'http://127.0.0.1:7897'
$repo = "evenbsies/ttboys"
$dir = "C:\Users\Administrator\Doubao\chats\2026-10-02\new-chat\tvbox-daily"

# enumerate all local files (handles Chinese names), skip the push script itself
$files = @()
foreach ($f in [IO.Directory]::GetFiles($dir)) {
  $name = [IO.Path]::GetFileName($f)
  if ($name -like "_push*" -or $name -like "_*") { continue }
  $files += $name
}
$files += ".github/workflows/daily.yml"

function Encode-Path([string]$p) {
  return (($p -split '/') | ForEach-Object { [uri]::EscapeDataString($_) }) -join '/'
}

"== auth status =="
& $gh auth status 2>&1 | Out-String | Write-Output
"== files to push: $($files.Count) =="

foreach ($f in $files) {
  $full = Join-Path $dir $f
  if (-not (Test-Path $full)) { "SKIP(local missing) $f"; continue }
  $path = Encode-Path $f
  $sha = ""
  try {
    $sha = (& $gh api "repos/$repo/contents/$path" --jq '.sha' 2>$null).Trim()
  } catch { $sha = "" }
  $b64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes($full))
  $body = @{ message = "update: $f"; content = $b64 }
  if ($sha) { $body.sha = $sha }
  $bodyFile = Join-Path $env:TEMP "gh_body.json"
  $json = $body | ConvertTo-Json -Compress
  [IO.File]::WriteAllText($bodyFile, $json, (New-Object Text.UTF8Encoding $false))
  try {
    & $gh api -X PUT "repos/$repo/contents/$path" --input $bodyFile 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
      $act = "overwrite"; if (-not $sha) { $act = "create" }
      "OK   $f ($act)"
    } else {
      "FAIL $f (exit $LASTEXITCODE)"
    }
  } catch {
    "FAIL $f : $($_.Exception.Message)"
  }
  Remove-Item $bodyFile -Force -ErrorAction SilentlyContinue
}
""
"ALL DONE."
