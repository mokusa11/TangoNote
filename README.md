# 词笺 TangoNote

词笺是一款与 DictTango 配合使用的本地笔记词典，可在浏览器中管理 MyNote 笔记、分组附件和高光规则，并支持通过 OneDrive 手动同步数据。

## Windows 快速开始

1. 安装 Python 3.10 或更高版本。
2. 运行 `程序\启动笔记程序.bat`。
3. 在浏览器打开 `http://127.0.0.1:8766/`。

程序不依赖第三方 Python 包。详细说明、Android/Termux 运行方式及手动更新说明见[程序 README](程序/README.md)；同步实现细节见[数据同步功能开发说明](数据同步功能开发说明.md)。

## 开发与测试

```powershell
cd 程序
python -B -m unittest -q test_app
```

## 本地数据与隐私

个人设置、笔记、高光规则和旧数据库不属于源代码，不应提交到仓库。程序运行时会在本机创建相应的数据文件；这些文件也已列入 `.gitignore`。发布 ZIP 同样会排除个人数据。
