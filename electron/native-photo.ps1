param([string]$CameraLabel, [double]$AspectRatio)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$capture = $null
$stream = $null
$reader = $null
try {
  Add-Type -AssemblyName System.Runtime.WindowsRuntime
  $null = [Windows.Media.Capture.MediaCapture, Windows.Media.Capture, ContentType=WindowsRuntime]
  $null = [Windows.Media.Capture.MediaCaptureInitializationSettings, Windows.Media.Capture, ContentType=WindowsRuntime]
  $null = [Windows.Devices.Enumeration.DeviceInformation, Windows.Devices.Enumeration, ContentType=WindowsRuntime]
  $null = [Windows.Devices.Enumeration.DeviceInformationCollection, Windows.Devices.Enumeration, ContentType=WindowsRuntime]
  $null = [Windows.Storage.Streams.InMemoryRandomAccessStream, Windows.Storage.Streams, ContentType=WindowsRuntime]
  $null = [Windows.Storage.Streams.DataReader, Windows.Storage.Streams, ContentType=WindowsRuntime]
  $null = [Windows.Media.MediaProperties.ImageEncodingProperties, Windows.Media.MediaProperties, ContentType=WindowsRuntime]
  $operationMethod = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.IsGenericMethodDefinition -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } | Select-Object -First 1
  $actionMethod = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and -not $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncAction' } | Select-Object -First 1
  function Await-Operation($operation, [Type]$resultType) { $task = $operationMethod.MakeGenericMethod($resultType).Invoke($null,@($operation)); $task.Wait(); return $task.Result }
  function Await-Action($action) { $task = $actionMethod.Invoke($null,@($action)); $task.Wait() }
  $devices = Await-Operation ([Windows.Devices.Enumeration.DeviceInformation]::FindAllAsync([Windows.Devices.Enumeration.DeviceClass]::VideoCapture)) ([Windows.Devices.Enumeration.DeviceInformationCollection])
  $name = $CameraLabel -replace '\s*\([0-9a-fA-F]{4}:[0-9a-fA-F]{4}\)$', ''
  $matches = @($devices | Where-Object { $_.Name -eq $name })
  if ($matches.Count -ne 1) { throw 'Cannot uniquely identify the selected camera for native photography.' }
  $settings = New-Object Windows.Media.Capture.MediaCaptureInitializationSettings
  $settings.VideoDeviceId = $matches[0].Id
  $settings.StreamingCaptureMode = [Windows.Media.Capture.StreamingCaptureMode]::Video
  # Auto also supports cameras without a separate hardware photo stream.
  $settings.PhotoCaptureSource = [Windows.Media.Capture.PhotoCaptureSource]::Auto
  $capture = New-Object Windows.Media.Capture.MediaCapture
  Await-Action ($capture.InitializeAsync($settings))
  $formats = @($capture.VideoDeviceController.GetAvailableMediaStreamProperties([Windows.Media.Capture.MediaStreamType]::Photo) | Where-Object {
    $_.Width -gt 0 -and $_.Height -gt 0 -and [Math]::Abs(([double]$_.Width / $_.Height) / $AspectRatio - 1) -lt 0.001
  })
  if (-not $formats.Count) { throw 'No native photo format matches the calibrated preview aspect ratio.' }
  $selected = $formats | Sort-Object { [long]$_.Width * $_.Height } -Descending | Select-Object -First 1
  Await-Action ($capture.VideoDeviceController.SetMediaStreamPropertiesAsync([Windows.Media.Capture.MediaStreamType]::Photo,$selected))
  $stream = New-Object Windows.Storage.Streams.InMemoryRandomAccessStream
  Await-Action ($capture.CapturePhotoToStreamAsync([Windows.Media.MediaProperties.ImageEncodingProperties]::CreatePng(),$stream))
  if ($stream.Size -gt 12000000) { throw 'Native photo exceeds the supported lossless upload size.' }
  $reader = New-Object Windows.Storage.Streams.DataReader($stream.GetInputStreamAt(0))
  $loaded = Await-Operation ($reader.LoadAsync([uint32]$stream.Size)) ([uint32])
  if ($loaded -ne $stream.Size) { throw 'Incomplete native photo.' }
  $bytes = New-Object byte[] ([int]$stream.Size)
  $reader.ReadBytes($bytes)
  $result = @{image='data:image/png;base64,'+[Convert]::ToBase64String($bytes);method='windows-native-photo';captured_at=[DateTime]::UtcNow.ToString('o')}
} catch {
  $result = @{error=$_.Exception.GetBaseException().Message}
} finally {
  if ($reader) { $reader.Dispose() }
  if ($stream) { $stream.Dispose() }
  if ($capture) { $capture.Dispose() }
}
# Release the device before returning the image to Electron.
$result | ConvertTo-Json -Compress
