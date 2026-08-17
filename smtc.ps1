# Streams the current Windows media session as newline-delimited JSON.
#
# This is the zero-setup half of nowwatching: every Chromium tab playing video
# registers a System Media Transport Controls session, and so does VLC, Spotify
# and anything else that implements SMTC. Reading it needs no browser extension,
# no permissions and no per-site adapters.
#
# Why a long-lived PowerShell process rather than a pip package: SMTC is WinRT,
# which Python cannot reach without a dependency (winsdk). Spawning powershell
# once and streaming lines keeps the daemon's standard-library-only promise, and
# costs one process instead of one process per poll.
#
# Emits one compact JSON object per line. `null` fields mean "not reported".
# Position is a snapshot, not a live clock: SMTC only refreshes it when the app
# pushes an update, so `positionAt` is included and the reader extrapolates.

param(
    [double]$IntervalSeconds = 1.0
)

$ErrorActionPreference = 'Stop'
$OutputEncoding = [Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Add-Type -AssemblyName System.Runtime.WindowsRuntime

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() |
    Where-Object {
        $_.Name -eq 'AsTask' -and
        $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    })[0]

function Await($WinRtTask, $ResultType) {
    $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
    $netTask = $asTask.Invoke($null, @($WinRtTask))
    $netTask.Wait(-1) | Out-Null
    $netTask.Result
}

$null = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType = WindowsRuntime]
$null = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties, Windows.Media.Control, ContentType = WindowsRuntime]

$mgrType = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]
$propType = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties]

try {
    $mgr = Await ($mgrType::RequestAsync()) ($mgrType)
} catch {
    # One line so the reader gets a reason rather than a silent empty stream.
    Write-Output (ConvertTo-Json -Compress @{ error = "SMTC unavailable: $($_.Exception.Message)" })
    exit 1
}

Write-Output (ConvertTo-Json -Compress @{ ready = $true })

# Unix epoch seconds, so the Python side can compare against time.time()
# without parsing a locale-formatted date.
$epoch = [datetime]'1970-01-01T00:00:00Z'

while ($true) {
    $payload = $null
    try {
        # GetCurrentSession() returns the session Windows considers focused, and
        # it goes null in cases where sessions plainly exist: a paused tab that
        # lost focus, or several players open at once. Enumerating and preferring
        # a Playing session is what keeps presence alive through those.
        $s = $mgr.GetCurrentSession()
        $all = @($mgr.GetSessions())
        if ($null -eq $s -and $all.Count -gt 0) {
            $s = $all | Where-Object {
                $_.GetPlaybackInfo().PlaybackStatus -eq 'Playing'
            } | Select-Object -First 1
            if ($null -eq $s) { $s = $all[0] }
        }

        if ($null -eq $s) {
            $payload = @{ session = $null; sessionCount = $all.Count }
        } else {
            $info = $s.GetPlaybackInfo()
            $tl = $s.GetTimelineProperties()

            $title = $null; $artist = $null; $hasArt = $false
            $album = $null; $subtitle = $null; $track = 0
            try {
                $p = Await ($s.TryGetMediaPropertiesAsync()) ($propType)
                $title = $p.Title
                $artist = $p.Artist
                $hasArt = ($null -ne $p.Thumbnail)
                # A site implementing the Media Session API properly puts the
                # show in AlbumTitle and the episode in Title or Subtitle, and
                # sometimes the episode number in TrackNumber. Sites that set
                # nothing leave all of these empty, and Chromium falls back to
                # the page title, so reading them costs nothing and is the only
                # way to get a season or episode without the extension.
                $album = $p.AlbumTitle
                $subtitle = $p.Subtitle
                $track = $p.TrackNumber
            } catch {
                # Media props can transiently fail while a tab is switching
                # tracks. Timeline and status are still worth reporting.
            }

            # EndTime is the duration; StartTime is almost always zero but is
            # subtracted anyway so a clipped stream reports its real length.
            $duration = $null
            if ($tl.EndTime -gt $tl.StartTime) {
                $duration = ($tl.EndTime - $tl.StartTime).TotalSeconds
            }

            $positionAt = $null
            if ($tl.LastUpdatedTime -and $tl.LastUpdatedTime.Year -gt 1971) {
                $positionAt = ($tl.LastUpdatedTime.UtcDateTime - $epoch).TotalSeconds
            }

            $payload = @{
                session      = $true
                sessionCount = $all.Count
                app          = [string]$s.SourceAppUserModelId
                status       = [string]$info.PlaybackStatus
                title        = $title
                artist       = $artist
                album        = $album
                subtitle     = $subtitle
                track        = $track
                position     = ($tl.Position - $tl.StartTime).TotalSeconds
                duration     = $duration
                positionAt   = $positionAt
                hasArt       = $hasArt
            }
        }
    } catch {
        $payload = @{ error = [string]$_.Exception.Message }
    }

    Write-Output (ConvertTo-Json -Compress $payload)
    [Console]::Out.Flush()
    Start-Sleep -Seconds $IntervalSeconds
}
