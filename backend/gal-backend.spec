from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, collect_data_files, copy_metadata
project = Path(SPECPATH).parent
a = Analysis([str(project / 'backend' / 'launcher.py')],
    pathex=[str(project / 'backend')],
    # LangChain / LangGraph 使用动态导入，冻结版必须附带节点、序列化及模型模块。
    hiddenimports=collect_submodules('keyring.backends') + collect_submodules('windows_capture') + collect_submodules('langchain.agents') + collect_submodules('langchain_core') + collect_submodules('langgraph') + ['uvicorn.logging','uvicorn.loops.auto','uvicorn.protocols.http.h11_impl','uvicorn.protocols.websockets.websockets_impl','uvicorn.protocols.websockets.wsproto_impl','uvicorn.protocols.websockets.websockets_sansio_impl','uvicorn.lifespan.on'],
    excludes=['pytest','tkinter','matplotlib','PyQt5','PySide6'],
    # 模型适配与框架会读取包元数据；递归包含实际依赖的版本资料。
    binaries=[], datas=copy_metadata('langchain', recursive=True) + collect_data_files('langchain_core'), hookspath=[], runtime_hooks=[])
pyz = PYZ(a.pure)
exe = EXE(pyz,a.scripts,[],exclude_binaries=True,name='gal-backend',console=True)
coll = COLLECT(exe,a.binaries,a.datas,name='gal-backend')
