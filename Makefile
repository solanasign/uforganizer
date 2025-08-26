# Makefile for Universal File Organizer (UFO)

PACKAGE=uforganizer

.PHONY: install uninstall dev clean

install:
	@echo "Installing $(PACKAGE)..."
	pip install .

uninstall:
	@echo "Uninstalling $(PACKAGE)..."
	pip uninstall -y $(PACKAGE)

dev:
	@echo "Installing $(PACKAGE) in editable (dev) mode..."
	pip install -e .

clean:
	@echo "Cleaning build artifacts..."
	rm -rf build dist *.egg-info
	find . -type d -name "__pycache__" -exec rm -rf {} +