@echo off
echo ==================================================
echo         ProxyHub Standalone Binary Builder
echo ==================================================
echo.
echo Installing PyInstaller dependency...
pip install pyinstaller
echo.
echo Compiling ProxyHub.exe using proxyhub.spec...
pyinstaller --clean proxyhub.spec
echo.
echo Build finished! Check the 'dist' directory for 'ProxyHub.exe'.
pause
