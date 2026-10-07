from setuptools import find_packages, setup

setup(
    name="scenic-reservation",
    version="0.1.0",
    description="景区分时预约与容量结算服务",
    package_dir={"": "src"},
    packages=find_packages("src"),
    python_requires=">=3.11",
)
