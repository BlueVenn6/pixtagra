# PixTagra application branding

`PixTagra_App_Icon.png` is the supplied original 1024 × 1024 RGBA logo.
It is kept unchanged as the source of the application's only official icon.

`PixTagra_App_Icon.ico` contains 16, 24, 32, 48, 64, 128 and 256 pixel frames.
Each frame matches the original resized with Pillow's LANCZOS filter, including
its alpha channel. No crop, recoloring, background or redesign is applied.

To reproduce the existing ICO with the already required Pillow dependency:

```powershell
python scripts/generate_app_icon.py
```

The directory lists the 256px frame first because this Tk Windows runtime
uses the first image. This avoids enlarging the 16px frame on high DPI screens;
the image payloads themselves remain unchanged.

PyInstaller embeds the ICO in `PixTagra.exe` and ships the same ICO under
`_internal/assets/branding/` for Tk windows. NSIS uses that verified payload icon
for setup/uninstall and uses the corresponding executable's icon for Start
menu shortcuts. The existing installer does not create a desktop shortcut.
