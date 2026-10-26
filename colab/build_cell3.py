"""Rebuild colab/cell3_booth.py from engine_source.py + ../index.html.
The page HTML is embedded in cell 3 so the Colab studio serves the same page as GitHub Pages.
Run from the repo root:  python colab/build_cell3.py"""
import pathlib
root = pathlib.Path(__file__).resolve().parent.parent
engine = (root / "colab" / "engine_source.py").read_text()
page = (root / "index.html").read_text().strip()
assert '"""' not in page, "index.html must not contain triple quotes"
cell = engine.replace('PAGE = r"""__PAGE__"""', 'PAGE = r"""' + page + '"""')
compile(cell, "cell3", "exec")
(root / "colab" / "cell3_booth.py").write_text(cell)
print("wrote colab/cell3_booth.py", len(cell), "chars")
