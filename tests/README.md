# 测试

在项目目录执行：

```bash
PYTHONPATH=src python -m unittest discover -s tests
```

Windows PowerShell：

```powershell
$env:PYTHONPATH="src"
python -m unittest discover -s tests
```

测试使用工作区内的独立临时目录，不需要模型 API 或真实账号。
