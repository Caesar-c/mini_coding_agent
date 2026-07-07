.PHONY: install build clean clean-exe publish publish-test lint test exe help

# ─── 开发 ───────────────────────────────────────────────────────
install:  ## 本地开发安装（editable 模式）
	.venv/bin/pip install -e .

test:  ## 运行所有测试
	PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -t . -v

lint:  ## 运行 lint + 格式化检查
	.venv/bin/ruff check src/ tests/
	.venv/bin/ruff format --check src/ tests/

format:  ## 自动修复 lint + 格式化
	.venv/bin/ruff check --fix src/ tests/
	.venv/bin/ruff format src/ tests/

# ─── 构建 ───────────────────────────────────────────────────────
build:  ## 构建 wheel + sdist（产物: dist/*.whl, dist/*.tar.gz）
	.venv/bin/pip install build
	.venv/bin/python -m build

clean:  ## 清理 wheel 构建产物（不影响 PyInstaller 产物）
	rm -rf dist/*.whl dist/*.tar.gz build/ *.egg-info src/*.egg-info

# ─── 发布 ───────────────────────────────────────────────────────
publish-test: build  ## 发布到 TestPyPI（先测试）
	.venv/bin/pip install twine
	.venv/bin/twine upload --repository testpypi dist/*.whl dist/*.tar.gz

publish: build  ## 发布到 PyPI
	.venv/bin/pip install twine
	.venv/bin/twine upload dist/*.whl dist/*.tar.gz

# ─── 独立可执行文件（PyInstaller）─────────────────────────────
exe:  ## 用 PyInstaller 打包（产物: dist/mini-agent/）
	.venv/bin/pip install pyinstaller
	.venv/bin/pyinstaller pyinstaller.spec

clean-exe:  ## 清理 PyInstaller 产物
	rm -rf dist/mini-agent/ build/mini-agent/

# ─── 帮助 ───────────────────────────────────────────────────────
help:  ## 显示帮助
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-15s\033[0m %s\n", $$1, $$2}'
