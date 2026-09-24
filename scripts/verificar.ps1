# Roda localmente exatamente o que o CI roda.
#
# O objetivo é que ninguém descubra no push o que dava para descobrir em 40
# segundos na própria máquina.

$ErrorActionPreference = "Continue"
Set-Location (Join-Path $PSScriptRoot "..")

$py = if (Test-Path ".venv\Scripts\python.exe") { ".venv\Scripts\python.exe" } else { "python" }
$falhas = 0

function Passo($nome, [scriptblock]$acao) {
    Write-Host ""
    Write-Host "-- $nome ------------------------------------------"
    & $acao
    if ($LASTEXITCODE -eq 0) {
        Write-Host "   ok"
    } else {
        Write-Host "   FALHOU" -ForegroundColor Red
        $script:falhas++
    }
}

Passo "lint"        { & $py -m ruff check . }
Passo "formatacao"  { & $py -m ruff format --check . }
Passo "tipos"       { & $py -m mypy app }
Passo "testes"      { & $py -m pytest -q }

if ($env:DATABASE_URL) {
    Passo "deriva de schema" { & $py -m alembic check }
} else {
    Write-Host ""
    Write-Host "-- deriva de schema ------------------------------"
    Write-Host "   ignorado: defina DATABASE_URL para rodar 'alembic check'"
}

Write-Host ""
if ($falhas -eq 0) {
    Write-Host "tudo verde." -ForegroundColor Green
} else {
    Write-Host "$falhas passo(s) falharam." -ForegroundColor Red
    exit 1
}
