@echo off
chcp 65001 >nul
cd /d "%~dp0"
python make_icon.py
rem 排除 PyMuPDF 等库顺带引入、但本程序用不到的大型依赖，exe 可小一半
python -m PyInstaller --noconfirm --onefile --windowed ^
  --name "GLM-MCP-文档对比agent" ^
  --icon "%~dp0app.ico" --add-data "%~dp0app.ico;." --add-data "%~dp0skills;skills" ^
  --collect-submodules pygments ^
  --exclude-module numpy --exclude-module scipy --exclude-module matplotlib --exclude-module pandas ^
  --exclude-module PIL --exclude-module lxml --exclude-module cryptography --exclude-module IPython ^
  --exclude-module tkinter --exclude-module pytest --exclude-module sympy --exclude-module setuptools ^
  --workpath build --specpath build --distpath dist ^
  glm_client.py
echo.
echo 输出位置: %~dp0dist\GLM-MCP-文档对比agent.exe
echo 自检: "dist\GLM-MCP-文档对比agent.exe" --selftest some.pdf   结果见 dist\logs\selftest.txt
pause
