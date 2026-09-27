# 压测期间采样服务端与压测端的进程级 CPU / 内存，用于判断瓶颈归属。
# 用法（前台示例，压测时长 150s 时给 200s）：
#   powershell -File scripts/perf/sample-proc.ps1 -Seconds 200 -Interval 3
param(
    [int]$Seconds = 200,
    [int]$Interval = 3,
    [string[]]$Names = @('python', 'k6')
)

$cores = (Get-CimInstance Win32_ComputerSystem).NumberOfLogicalProcessors
Write-Output "logical_cores=$cores interval=${Interval}s"

$prev = @{}
$end = (Get-Date).AddSeconds($Seconds)
while ((Get-Date) -lt $end) {
    $ts = Get-Date -Format 'HH:mm:ss'
    $out = "$ts"
    foreach ($n in $Names) {
        $ps = Get-Process -Name $n -ErrorAction SilentlyContinue
        if ($ps) {
            $cpu = ($ps | Measure-Object -Property CPU -Sum).Sum
            $memMB = [math]::Round((($ps | Measure-Object -Property WorkingSet64 -Sum).Sum / 1MB), 0)
            $pct = '-'
            if ($prev.ContainsKey($n)) {
                $pct = [math]::Round((($cpu - $prev[$n]) / $Interval) * 100 / $cores, 1)
            }
            $prev[$n] = $cpu
            $out += "  |  $n cpu=$pct% mem=${memMB}MB"
        }
        else {
            $out += "  |  $n absent"
        }
    }
    Write-Output $out
    Start-Sleep -Seconds $Interval
}
