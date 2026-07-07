from setuptools import setup, find_packages

with open("requirements.txt") as f:
    install_requires = [line.strip() for line in f if line.strip() and not line.startswith("#")]

setup(
    name="dms",
    version="2.1.0",
    description="GMP / 21 CFR Part 11 compliant Document Management System for ERPNext v16",
    author="ErenAydin",
    author_email="aydineren1986@gmail.com",
    packages=find_packages(),
    zip_safe=False,
    include_package_data=True,
    install_requires=install_requires,
)
