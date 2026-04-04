from setuptools import setup

with open("requirements.txt") as f:
    requires = [l.strip() for l in f if l.strip() and not l.startswith("#")]

setup(
    name="scout",
    version="1.0.0",
    description="AI-powered web research CLI using local LLMs via LM Studio",
    py_modules=[
        "cli",
        "config",
        "models",
        "exceptions",
        "fetcher",
        "searcher",
        "llm",
        "dspy_llm",
        "reporter",
        "scheduler",
    ],
    install_requires=requires,
    python_requires=">=3.12",
    entry_points={
        "console_scripts": ["scout=cli:main"],
    },
)
