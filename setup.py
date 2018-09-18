#!/usr/bin/env python
from setuptools import setup

with open('README.md', 'r') as file:
    long_description = file.read()

setup(
    name="tap-paypal",
    version="0.1.0",
    description="Singer.io tap for extracting data from the PayPal Sync API",
    author="Milk Bar",
    url="https://github.com/milk-bar/tap-paypal",
    classifiers=["Programming Language :: Python :: 3 :: Only"],
    py_modules=["tap_paypal"],
    install_requires=[
        "singer-python>=5.0.12",
        "requests",
        "requests_oauthlib",
        "python-dateutil"
    ],
    entry_points="""
    [console_scripts]
    tap-paypal=tap_paypal:main
    """,
    packages=["tap_paypal"],
    package_data = {
        "schemas": ["tap_paypal/schemas/*.json"]
    },
    include_package_data=True,
)
