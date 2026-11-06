# Pagefile change record

A record of one machine's Windows setting, kept so that it can be restored. RepoMind does not need this change on another machine.

## State before the change

Read on 2026-11-06:

| Item | Value |
| --- | --- |
| `Win32_PageFileSetting` | `c:\pagefile.sys`, initial 0, maximum 0, which means system managed |
| `AutomaticManagedPagefile` | False, and it stays False |
| Registry `PagingFiles` | `c:\pagefile.sys 0 0` |
| Allocated now | 7,114 MB |
| In use, and peak since boot | 1,338 MB and 3,886 MB |
| Commit limit | 38.2 GB, and 45.0 GB seen earlier the same day |
| Committed, and free | 30.9 GB and 7.3 GB |
| Available physical memory | 13.6 GB |
| C: free | 525 GB of 930 GB |
| Other fixed drives | None |

## Why it changes

The pagefile did grow when it had to, to about 13.7 GB, and it shrank again. Windows logged no low virtual memory event, and no warning or error in the System log while the 7B run was stopped. So Windows does not need this change to stay healthy.

It is needed for one thing: a system-managed pagefile grows only on demand, so free commit stays near 7 GB until a load has already started. A model run is only started with 12 GB free, and Claude Code's memory guard, whose exact measure is not known, stopped a run with about 2 GB free. A larger fixed start gives that margin in advance. It uses disk space and no RAM.

## Apply

In PowerShell opened as administrator:

```powershell
Get-CimInstance Win32_PageFileSetting -Filter "Name='c:\\pagefile.sys'" | Set-CimInstance -Property @{InitialSize=[uint32]16384; MaximumSize=[uint32]32768}
```

## Restore

In PowerShell opened as administrator, then restart Windows, because Windows may need it to shrink a pagefile:

```powershell
Get-CimInstance Win32_PageFileSetting -Filter "Name='c:\\pagefile.sys'" | Set-CimInstance -Property @{InitialSize=[uint32]0; MaximumSize=[uint32]0}
```

## Check

No administrator rights needed:

```powershell
Get-CimInstance Win32_PageFileSetting | Select-Object Name, InitialSize, MaximumSize
Get-CimInstance Win32_PageFileUsage | Select-Object Name, AllocatedBaseSize, CurrentUsage, PeakUsage
$m = Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory
"available {0:N1} GB, commit free {1:N1} of {2:N1} GB" -f ($m.AvailableBytes/1GB), (($m.CommitLimit-$m.CommittedBytes)/1GB), ($m.CommitLimit/1GB)
```

If the setting shows 16384 and 32768 but the allocated size is still about 7 GB, the change waits for a restart.
