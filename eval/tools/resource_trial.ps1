<#
Small trial of one local model: a few questions, resource samples, and a clean stop.

It refuses to start when memory is short, never downloads a model, touches only RepoMind's
own Ollama container, and stops that container afterwards. Answers are saved after every
question. Running the same command again continues an interrupted trial.

    .\eval\tools\resource_trial.ps1 -CheckOnly
    .\eval\tools\resource_trial.ps1 -Model qwen2.5:7b -Questions 10

The start limit is the worst case added to what must stay free: the container's 8 GB limit,
1 GB for Python, and 3 GB of margin. Earlier, a run was killed with about 2 GB of commit space
left. A run that comes down to the 4 GB limit stops between questions and keeps its answers.
#>
param(
    [string]$Model = 'qwen2.5:7b',
    [int]$Questions = 10,
    [string]$Language = 'tr',
    [double]$MinFreeBeforeStartGb = 12,
    [double]$MinFreeDuringRunGb = 4,
    [switch]$CheckOnly
)

$root = (Resolve-Path "$PSScriptRoot\..\..").Path
$python = "$root\.venv\Scripts\python.exe"
$name = $Model -replace '[^A-Za-z0-9.-]', '-'
$output = "$root\eval\results\trial-$name-$Language-$($Questions)q.json"
$samples = "$root\eval\results\resources\stats-trial-$name-$Language-$($Questions)q.txt"
Set-Location $root

$os = Get-CimInstance Win32_OperatingSystem
$physical = $os.FreePhysicalMemory / 1MB
$commit = $os.FreeVirtualMemory / 1MB
"Free physical memory {0:N1} GB, free commit space {1:N1} GB." -f $physical, $commit
if ([math]::Min($physical, $commit) -lt $MinFreeBeforeStartGb) {
    "Not ready: this trial needs at least $MinFreeBeforeStartGb GB of both."
    exit 1
}
if ($CheckOnly) {
    "Ready: both are above $MinFreeBeforeStartGb GB. Nothing was started."
    exit 0
}

docker compose --profile llm up -d --no-deps ollama
if ($LASTEXITCODE -ne 0) { "Could not start RepoMind's Ollama container."; exit 1 }
$installed = docker exec repomind-ollama ollama list | Out-String
if ($installed -notmatch [regex]::Escape($Model)) {
    "Not starting: $Model is not installed, and this script does not download models."
    docker compose stop ollama
    exit 1
}

$sampler = Start-Job -ScriptBlock {
    param($file)
    while ($true) {
        $ollama = docker stats repomind-ollama --no-stream --format '{{.MemUsage}} {{.CPUPerc}}'
        # The venv's python.exe is a small launcher. The interpreter is a child with the same command line.
        $own = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like '*repomind*' }
        $python_mb = [math]::Round((($own | Measure-Object WorkingSetSize -Maximum).Maximum) / 1MB)
        $system = Get-CimInstance Win32_OperatingSystem
        Add-Content $file ("$ollama | python ${python_mb}MB | free {0:N1}GB commit {1:N1}GB" -f ($system.FreePhysicalMemory / 1MB), ($system.FreeVirtualMemory / 1MB))
        Start-Sleep -Seconds 20
    }
} -ArgumentList $samples

try {
    $env:REPOMIND_LLM_MODEL = $Model
    $env:HF_HUB_DISABLE_SYMLINKS_WARNING = '1'
    & $python -m repomind evaluate-ask --dataset "$root\eval\questions_v2.yaml" --languages $Language `
        --limit $Questions --min-free-memory-gb $MinFreeDuringRunGb --output $output
}
finally {
    Stop-Job $sampler
    Remove-Job $sampler
    docker exec repomind-ollama ollama stop $Model | Out-Null
    docker compose stop ollama
}
"Answers: $output"
"Samples: $samples"
