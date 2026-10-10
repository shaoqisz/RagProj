[CmdletBinding()]
param(
	[string]$InputFile = "SRS_baseline_25.76(VM14.0 SVAL-R).xlsx",
	[string]$OutputFile = "SRS_baseline_25.76(VM14.0 SVAL-R)_preprocessed.xlsx",
	[string]$WorksheetName = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Add-Type -AssemblyName System.IO.Compression.FileSystem

function Get-CellText {
	param(
		[Parameter(Mandatory = $true)]
		[System.Xml.XmlElement]$Cell,

		[Parameter(Mandatory = $true)]
		[string[]]$SharedStrings
	)

	$type = $Cell.GetAttribute("t")

	if ($type -eq "s") {
		$vNode = $Cell.SelectSingleNode("./d:v", $script:NsManager)
		if (-not $vNode) { return "" }
		$idx = 0
		if (-not [int]::TryParse($vNode.InnerText, [ref]$idx)) { return "" }
		if ($idx -lt 0 -or $idx -ge $SharedStrings.Count) { return "" }
		return $SharedStrings[$idx]
	}

	if ($type -eq "inlineStr") {
		$tNode = $Cell.SelectSingleNode("./d:is/d:t", $script:NsManager)
		if ($tNode) { return $tNode.InnerText }
		return ""
	}

	if ($type -eq "str") {
		$vNode = $Cell.SelectSingleNode("./d:v", $script:NsManager)
		if ($vNode) { return $vNode.InnerText }
		return ""
	}

	$plainNode = $Cell.SelectSingleNode("./d:v", $script:NsManager)
	if ($plainNode) { return $plainNode.InnerText }
	return ""
}

function Set-CellTextInline {
	param(
		[Parameter(Mandatory = $true)]
		[System.Xml.XmlElement]$Cell,

		[Parameter(Mandatory = $true)]
		[string]$Text,

		[Parameter(Mandatory = $true)]
		[System.Xml.XmlDocument]$SheetXml
	)

	$vNode = $Cell.SelectSingleNode("./d:v", $script:NsManager)
	if ($vNode) { [void]$Cell.RemoveChild($vNode) }

	$isNode = $Cell.SelectSingleNode("./d:is", $script:NsManager)
	if ($isNode) { [void]$Cell.RemoveChild($isNode) }

	$Cell.SetAttribute("t", "inlineStr")

	$isElem = $SheetXml.CreateElement("is", $script:MainNs)
	$tElem = $SheetXml.CreateElement("t", $script:MainNs)
	$spaceAttr = $SheetXml.CreateAttribute("xml", "space", "http://www.w3.org/XML/1998/namespace")
	$spaceAttr.Value = "preserve"
	[void]$tElem.Attributes.Append($spaceAttr)
	$tElem.InnerText = $Text
	[void]$isElem.AppendChild($tElem)
	[void]$Cell.AppendChild($isElem)
}

function Get-HierarchyKeys {
	param(
		[Parameter(Mandatory = $true)]
		[string]$BaseSection
	)

	$parts = $BaseSection.Split('.')
	$keys = New-Object System.Collections.Generic.List[string]
	for ($i = 1; $i -le $parts.Count; $i++) {
		$keys.Add(($parts[0..($i - 1)] -join '.'))
	}
	return $keys
}

function Get-ParentKeysForRequirementSection {
	param(
		[Parameter(Mandatory = $true)]
		[string]$Section
	)

	# Rule:
	# 1) Remove everything after the first '-'
	# 2) Match only dotted parent hierarchy of that head section
	#    - 5.6.1.0-12... => parents: 5, 5.6, 5.6.1
	#    - 5.6.1.0.8-1... => parents: 5, 5.6, 5.6.1, 5.6.1.0
	$head = $Section
	$dashIdx = $head.IndexOf('-')
	if ($dashIdx -ge 0) {
		$head = $head.Substring(0, $dashIdx)
	}
	$head = $head.Trim('.')
	if (-not $head) {
		return @()
	}

	$allKeys = @(Get-HierarchyKeys -BaseSection $head)
	if ($allKeys.Count -le 1) {
		return @()
	}

	# Exclude the head itself, keep only parents.
	return $allKeys[0..($allKeys.Count - 2)]
}

function Parse-BCell {
	param(
		[Parameter(Mandatory = $true)]
		[string]$Text
	)

	$trimmed = $Text.Trim()
	if (-not $trimmed) { return $null }

	# Support complex requirement ids such as 5.6.1.0-12.0-1.0-4.0-2
	$m = [regex]::Match($trimmed, '^(?<sec>\d[\d\.-]*)\s+(?<body>.+)$')
	if (-not $m.Success) { return $null }

	$sec = $m.Groups['sec'].Value
	if ($sec -notmatch '^[0-9][0-9\.-]*$') { return $null }

	return [pscustomobject]@{
		Section = $sec
		Body    = $m.Groups['body'].Value.Trim()
	}
}

function Resolve-WorksheetPath {
	param(
		[Parameter(Mandatory = $true)]
		[xml]$WorkbookXml,

		[Parameter(Mandatory = $true)]
		[xml]$WorkbookRelsXml,

		[Parameter(Mandatory = $false)]
		[string]$WorksheetName
	)

	$ns = New-Object System.Xml.XmlNamespaceManager($WorkbookXml.NameTable)
	$ns.AddNamespace("d", "http://schemas.openxmlformats.org/spreadsheetml/2006/main")

	$sheets = @($WorkbookXml.SelectNodes("//d:sheets/d:sheet", $ns))
	if ($sheets.Count -eq 0) {
		throw "No worksheet found in workbook.xml"
	}

	$targetSheet = $null
	if ($WorksheetName) {
		foreach ($s in $sheets) {
			if ($s.GetAttribute("name") -eq $WorksheetName) {
				$targetSheet = $s
				break
			}
		}
		if (-not $targetSheet) {
			throw "Worksheet '$WorksheetName' not found."
		}
	}
	else {
		$targetSheet = $sheets[0]
	}

	$rid = $targetSheet.GetAttribute("id", "http://schemas.openxmlformats.org/officeDocument/2006/relationships")
	if (-not $rid) {
		throw "Could not resolve relationship id for worksheet."
	}

	$relsNs = New-Object System.Xml.XmlNamespaceManager($WorkbookRelsXml.NameTable)
	$relsNs.AddNamespace("r", "http://schemas.openxmlformats.org/package/2006/relationships")
	$relNode = $WorkbookRelsXml.SelectSingleNode("//r:Relationship[@Id='$rid']", $relsNs)
	if (-not $relNode) {
		throw "Could not resolve worksheet relationship '$rid'."
	}

	$target = $relNode.GetAttribute("Target")
	if (-not $target) {
		throw "Relationship '$rid' has empty Target."
	}

	if ($target.StartsWith("/")) {
		return ("xl" + $target)
	}
	return ("xl/" + $target)
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$inputPath = if ([IO.Path]::IsPathRooted($InputFile)) { $InputFile } else { Join-Path $scriptDir $InputFile }
$outputPath = if ([IO.Path]::IsPathRooted($OutputFile)) { $OutputFile } else { Join-Path $scriptDir $OutputFile }

if (-not (Test-Path -LiteralPath $inputPath)) {
	throw "Input file not found: $inputPath"
}

$tempRoot = Join-Path ([IO.Path]::GetTempPath()) ("srs_preprocess_" + [Guid]::NewGuid().ToString("N"))
New-Item -Path $tempRoot -ItemType Directory | Out-Null

try {
	try {
		[IO.Compression.ZipFile]::ExtractToDirectory($inputPath, $tempRoot)
	}
	catch [System.IO.IOException] {
		throw "Cannot open xlsx file. Please close Excel (and any preview pane) before running. File: $inputPath"
	}

	$workbookPath = Join-Path $tempRoot "xl/workbook.xml"
	$workbookRelsPath = Join-Path $tempRoot "xl/_rels/workbook.xml.rels"

	if (-not (Test-Path -LiteralPath $workbookPath)) { throw "Invalid xlsx: missing xl/workbook.xml" }
	if (-not (Test-Path -LiteralPath $workbookRelsPath)) { throw "Invalid xlsx: missing xl/_rels/workbook.xml.rels" }

	[xml]$workbookXml = Get-Content -LiteralPath $workbookPath -Raw
	[xml]$workbookRelsXml = Get-Content -LiteralPath $workbookRelsPath -Raw

	$sheetRelPath = Resolve-WorksheetPath -WorkbookXml $workbookXml -WorkbookRelsXml $workbookRelsXml -WorksheetName $WorksheetName
	$sheetPath = Join-Path $tempRoot $sheetRelPath

	if (-not (Test-Path -LiteralPath $sheetPath)) {
		throw "Worksheet file not found: $sheetRelPath"
	}

	$sharedStringsPath = Join-Path $tempRoot "xl/sharedStrings.xml"
	$sharedStrings = @()
	if (Test-Path -LiteralPath $sharedStringsPath) {
		[xml]$ssXml = Get-Content -LiteralPath $sharedStringsPath -Raw
		$ssNs = New-Object System.Xml.XmlNamespaceManager($ssXml.NameTable)
		$ssNs.AddNamespace("d", "http://schemas.openxmlformats.org/spreadsheetml/2006/main")
		foreach ($si in $ssXml.SelectNodes("//d:si", $ssNs)) {
			$texts = $si.SelectNodes(".//d:t", $ssNs)
			if ($texts.Count -eq 0) {
				$sharedStrings += ""
			}
			else {
				$sharedStrings += (($texts | ForEach-Object { $_.InnerText }) -join "")
			}
		}
	}

	[xml]$sheetXml = Get-Content -LiteralPath $sheetPath -Raw
	$script:MainNs = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
	$script:NsManager = New-Object System.Xml.XmlNamespaceManager($sheetXml.NameTable)
	$script:NsManager.AddNamespace("d", $script:MainNs)

	$bCells = @($sheetXml.SelectNodes("//d:sheetData/d:row/d:c[starts-with(@r,'B')]", $script:NsManager))
	if ($bCells.Count -eq 0) {
		throw "No B-column cells found in target worksheet."
	}

	$chapterMap = @{}
	$updatedCount = 0

	foreach ($cell in $bCells) {
		$raw = Get-CellText -Cell $cell -SharedStrings $sharedStrings
		$parsed = Parse-BCell -Text $raw
		if (-not $parsed) { continue }

		$section = $parsed.Section
		$body = $parsed.Body

		if ($body -match '\s\|\s') {
			continue
		}

		if ($section -notmatch '-') {
			$chapterMap[$section] = $body
			continue
		}

		$parents = New-Object System.Collections.Generic.List[string]
		foreach ($key in (Get-ParentKeysForRequirementSection -Section $section)) {
			if ($chapterMap.ContainsKey($key)) {
				$parents.Add($chapterMap[$key])
			}
		}

		if ($parents.Count -eq 0) { continue }

		$newBody = ($parents -join " | ") + " | " + $body
		$newText = "$section $newBody"

		Set-CellTextInline -Cell $cell -Text $newText -SheetXml $sheetXml
		$updatedCount++
	}

	$sheetXml.Save($sheetPath)

	if (Test-Path -LiteralPath $outputPath) {
		Remove-Item -LiteralPath $outputPath -Force
	}

	[IO.Compression.ZipFile]::CreateFromDirectory($tempRoot, $outputPath, [IO.Compression.CompressionLevel]::Optimal, $false)
	Write-Host "Done. Updated $updatedCount rows in column B."
	Write-Host "Output: $outputPath"
}
finally {
	if (Test-Path -LiteralPath $tempRoot) {
		Remove-Item -LiteralPath $tempRoot -Recurse -Force
	}
}
