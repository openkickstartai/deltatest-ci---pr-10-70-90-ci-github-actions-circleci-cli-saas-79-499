from setuptools import setup

setup(
    name="deltatest",
    version="0.1.0",
    description="CI Test Impact Analysis Engine — only run tests affected by your changes",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    py_modules=["deltatest", "deltatest_plugin"],
    python_requires=">=3.8",
    install_requires=["pytest>=7.0"],
    entry_points={
        "console_scripts": ["deltatest=deltatest:main"],
        "pytest11": ["deltatest=deltatest_plugin"],
    },
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "Topic :: Software Development :: Testing",
        "Framework :: Pytest",
        "Programming Language :: Python :: 3",
    ],
    license="MIT",
)
