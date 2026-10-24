from setuptools import setup

setup(name='mhseals_config', version='0.1.0', packages=['mhseals_config'],
      install_requires=['PyYAML>=6'], data_files=[
          ('share/ament_index/resource_index/packages', ['resource/mhseals_config']),
          ('share/mhseals_config', ['package.xml'])])
