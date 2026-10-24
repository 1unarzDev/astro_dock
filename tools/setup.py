from setuptools import setup

setup(name='astro-run-config', version='0.1.0', py_modules=['astro_run_config'],
      data_files=[('share/astro-run-config', ['../config/run-defaults.yaml',
                                           '../config/run-catalog.yaml'])],
      install_requires=['mhseals_config==0.1.0', 'PyYAML>=6'],
      entry_points={'console_scripts': ['astro-run-config=astro_run_config:main']})
