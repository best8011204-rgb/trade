# 바이낸스 fstream 웹소켓 전용 SOCKS5 터널 유지 스크립트.
# 지역 차단으로 fstream 데이터 프레임이 안 오는 문제 회피용 — Oracle Cloud(비차단 리전) VPS로
# SSH 터널을 뚫어 로컬 127.0.0.1:1080에 SOCKS5 프록시를 띄운다.
# 연결이 끊기면 5초 후 자동 재접속. run_all.py 실행 전에 이 창을 먼저 띄워두고
# 계속 열어둔 채로 둘 것 (config.json의 ws_proxy가 이 프록시를 가리켜야 함).
#
# 사용법 (값 채운 뒤):
#   powershell -ExecutionPolicy Bypass -File scripts\start_ws_proxy.ps1 -KeyPath "C:\Users\HUN\.ssh\oracle_vps.key" -VpsHost "ubuntu@<PUBLIC_IP>"

param(
    [Parameter(Mandatory = $true)][string]$KeyPath,
    [Parameter(Mandatory = $true)][string]$VpsHost,
    [int]$LocalPort = 1080
)

Write-Host "SOCKS5 터널 시작: 127.0.0.1:$LocalPort -> $VpsHost (key: $KeyPath)"
Write-Host "종료하려면 이 창에서 Ctrl+C."

while ($true) {
    ssh -N -D $LocalPort `
        -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes `
        -i $KeyPath $VpsHost
    Write-Host "[$(Get-Date -Format 'HH:mm:ss')] 터널 끊김 — 5초 후 재접속..."
    Start-Sleep -Seconds 5
}
