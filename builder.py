import zipfile
from pathlib import Path
root = Path(r'c:\Users\user\PycharmProjects\DWGREADER')
addon = root / 'blender_addon' / 'dwgreader_underlay'
zip_path = root / 'blender_addon' / 'dwgreader_underlay.zip'
with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
    for path in addon.rglob('*.py'):
        if '__pycache__' in path.parts or 'vendor' in path.parts:
            continue
        zf.write(path, Path('dwgreader_underlay') / path.relative_to(addon))
    for path in (root / 'dxf_read').glob('*.py'):
        if path.name == 'build_hatch_underlay.py':
            continue
        zf.write(path, Path('dwgreader_underlay') / 'dxf_read' / path.name)
print(zip_path)