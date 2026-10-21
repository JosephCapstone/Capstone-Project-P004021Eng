Add-Type -AssemblyName System.Drawing
$folder='C:\Users\dylan\Documents\UNI\Capstone\Booth visuals'
$shots='C:\Users\dylan\Documents\ShareX\Screenshots\2026-10'
$bg=[System.Drawing.ColorTranslator]::FromHtml('#10171B')
$white=[System.Drawing.SolidBrush]::new([System.Drawing.ColorTranslator]::FromHtml('#F6F8FA'))
$cyan=[System.Drawing.SolidBrush]::new([System.Drawing.ColorTranslator]::FromHtml('#64CDD7'))
$muted=[System.Drawing.SolidBrush]::new([System.Drawing.ColorTranslator]::FromHtml('#B6CBD0'))
function WriteText($g,$text,$x,$y,$w,$h,$size,$bold,$brush){
 $style=if($bold){[System.Drawing.FontStyle]::Bold}else{[System.Drawing.FontStyle]::Regular}
 $font=[System.Drawing.Font]::new('Arial',$size,$style,[System.Drawing.GraphicsUnit]::Pixel)
 $format=[System.Drawing.StringFormat]::new()
 $g.DrawString($text,$font,$brush,[System.Drawing.RectangleF]::new($x,$y,$w,$h),$format)
 $font.Dispose();$format.Dispose()
}
function PlaceImage($g,$file,$dst,$crop){
 $img=[System.Drawing.Image]::FromFile((Join-Path $shots $file))
 if($null -eq $crop){$crop=[System.Drawing.Rectangle]::new(0,0,$img.Width,$img.Height)}
 $g.DrawImage($img,$dst,$crop,[System.Drawing.GraphicsUnit]::Pixel)
 $img.Dispose()
}
$codec=[System.Drawing.Imaging.ImageCodecInfo]::GetImageEncoders() | Where-Object MimeType -eq 'image/jpeg'
$ep=[System.Drawing.Imaging.EncoderParameters]::new(1)
$ep.Param[0]=[System.Drawing.Imaging.EncoderParameter]::new([System.Drawing.Imaging.Encoder]::Quality,[long]100)
foreach($n in 1,2){
 $bmp=[System.Drawing.Bitmap]::new(1920,1080)
 $g=[System.Drawing.Graphics]::FromImage($bmp)
 $g.Clear($bg)
 $g.TextRenderingHint=[System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit
 $g.InterpolationMode=[System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
 if($n -eq 1){
  WriteText $g 'DELTA UI — Understand scan changes' 72 48 1780 90 60 $true $white
  WriteText $g 'Clear visual information for users who need answers quickly.' 76 154 1770 60 32 $false $muted
  PlaceImage $g 'kit_L6iSKniaoM.png' ([System.Drawing.Rectangle]::new(72,250,852,448)) $null
  PlaceImage $g 'kit_3ubeYLrfZn.png' ([System.Drawing.Rectangle]::new(996,250,852,448)) $null
  WriteText $g 'Full scene' 76 718 800 50 34 $true $cyan
  WriteText $g 'Slice enabled' 1000 718 800 50 34 $true $cyan
  WriteText $g 'See what changed' 76 822 540 50 34 $true $cyan
  WriteText $g 'Colour distinguishes the baseline from recorded changes, helping users find areas to review.' 76 888 530 160 28 $false $white
  WriteText $g 'Reveal the interior' 692 822 540 50 34 $true $cyan
  WriteText $g 'Slicing exposes internal areas while keeping changes in the context of the scan.' 692 888 530 160 28 $false $white
  WriteText $g 'Keep the full picture' 1308 822 540 50 34 $true $cyan
  WriteText $g 'Explore different views and return to the full scene when more context is needed.' 1308 888 530 160 28 $false $white
 }else{
  WriteText $g 'DELTA UI — Clear site information' 72 48 1780 90 60 $true $white
  WriteText $g 'The selected location and its recorded measurements, together in one view.' 76 154 1770 60 32 $false $muted
  PlaceImage $g 'kit_mb4bEfKgk6.png' ([System.Drawing.Rectangle]::new(640,272,1208,713)) ([System.Drawing.Rectangle]::new(51,126,1374,811))
  WriteText $g 'Find the site quickly' 76 288 530 50 34 $true $cyan
  WriteText $g 'A highlighted region connects the selected site to its location in the scan.' 76 354 520 150 28 $false $white
  WriteText $g 'Read the evidence' 76 532 530 50 34 $true $cyan
  WriteText $g 'Site dimensions and mean displacement show the recorded change relative to the baseline.' 76 598 520 155 28 $false $white
  WriteText $g 'Inspect more closely' 76 790 530 50 34 $true $cyan
  WriteText $g 'Focus on a site or isolate it to examine the affected area.' 76 856 520 140 28 $false $white
  WriteText $g 'Mean displacement describes change relative to the baseline. Severity may be unassessed.' 76 1020 1760 42 22 $false $muted
 }
 $g.Dispose()
 $bmp.Save((Join-Path $folder ('Engenius-slide-'+$n+'-enhanced.jpg')),$codec,$ep)
 $bmp.Save((Join-Path $folder ('Engenius-slide-'+$n+'-enhanced.png')),[System.Drawing.Imaging.ImageFormat]::Png)
 $bmp.Dispose()
}
$ep.Dispose();$white.Dispose();$cyan.Dispose();$muted.Dispose()
