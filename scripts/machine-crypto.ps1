param([ValidateSet('encrypt','decrypt')][string]$Mode)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Security
$inputText = [Console]::In.ReadToEnd()
$scope = [System.Security.Cryptography.DataProtectionScope]::LocalMachine
if ($Mode -eq 'encrypt') {
    $bytes = [Text.Encoding]::UTF8.GetBytes($inputText)
    $result = [Security.Cryptography.ProtectedData]::Protect($bytes, $null, $scope)
    [Console]::Out.Write([Convert]::ToBase64String($result))
} else {
    $bytes = [Convert]::FromBase64String($inputText)
    $result = [Security.Cryptography.ProtectedData]::Unprotect($bytes, $null, $scope)
    [Console]::Out.Write([Text.Encoding]::UTF8.GetString($result))
}
