"""An abstraction over virtualenv and Conda environments."""

import codecs
import copy
import hashlib
import itertools
import json
import logging
import os
import shutil
import tarfile

import yaml
from django.conf import settings

from readthedocs.builds.constants import EXTERNAL
from readthedocs.config import PIP, SETUPTOOLS, ParseError
from readthedocs.config import parse as parse_yaml
from readthedocs.config.models import PythonInstall, PythonInstallRequirements
from readthedocs.doc_builder.config import load_yaml_config
from readthedocs.doc_builder.constants import DOCKER_IMAGE
from readthedocs.doc_builder.environments import DockerBuildEnvironment
from readthedocs.doc_builder.loader import get_builder_class
from readthedocs.projects.constants import LOG_TEMPLATE
from readthedocs.projects.models import Feature
from readthedocs.storage import build_tools_storage

log = logging.getLogger(__name__)


class PythonEnvironment:

    """An isolated environment into which Python packages can be installed."""

    def __init__(self, version, build_env, config=None):
        self.version = version
        self.project = version.project
        self.build_env = build_env
        if config:
            self.config = config
        else:
            self.config = load_yaml_config(version)
        # Compute here, since it's used a lot
        self.checkout_path = self.project.checkout_path(self.version.slug)

    def delete_existing_build_dir(self):
        # Handle deleting old build dir
        build_dir = os.path.join(
            self.venv_path(),
            'build',
        )
        if os.path.exists(build_dir):
            log.info(
                LOG_TEMPLATE,
                {
                    'project': self.project.slug,
                    'version': self.version.slug,
                    'msg': 'Removing existing build directory',
                }
            )
            shutil.rmtree(build_dir)

    def delete_existing_venv_dir(self):
        venv_dir = self.venv_path()
        # Handle deleting old venv dir
        if os.path.exists(venv_dir):
            log.info(
                LOG_TEMPLATE,
                {
                    'project': self.project.slug,
                    'version': self.version.slug,
                    'msg': 'Removing existing venv directory',
                }
            )
            shutil.rmtree(venv_dir)

    def install_build_tools(self):
        """
        Install all ``build.tools`` defined by the user in the config file.

        It uses ``asdf`` behind the scenes to manage all the tools and versions
        of them. These tools/versions are stored in the Cloud cache and are
        downloaded on each build (~50 - ~100Mb).

        If the requested tool/version is not present in the cache, it's
        installed via ``asdf`` on the fly.
        """
        if settings.RTD_DOCKER_COMPOSE:
            # Create a symlink for ``root`` user to use the same ``.asdf``
            # installation as the ``docs`` user. Required for local building
            # since everything is run as ``root`` when using Local Development
            # instance
            cmd = [
                'ln',
                '-s',
                os.path.join(settings.RTD_DOCKER_WORKDIR, '.asdf'),
                '/root/.asdf',
            ]
            self.build_env.run(
                *cmd,
                record=False,
            )

        for tool, version in self.config.build.tools.items():
            full_version = version.full_version  # e.g. 3.9 -> 3.9.7

            # TODO: generate the correct path for the Python version
            # see https://github.com/readthedocs/readthedocs.org/pull/8447#issuecomment-911562267
            # tool_path = f'{self.config.build.os}/{tool}/2021-08-30/{full_version}.tar.gz'
            tool_path = f'{self.config.build.os}-{tool}-{full_version}.tar.gz'
            tool_version_cached = build_tools_storage.exists(tool_path)
            if tool_version_cached:
                remote_fd = build_tools_storage.open(tool_path, mode='rb')
                with tarfile.open(fileobj=remote_fd) as tar:
                    # Extract it on the shared path between host and Docker container
                    extract_path = os.path.join(self.project.doc_path, 'tools')
                    tar.extractall(extract_path)

                    # Move the extracted content to the ``asdf`` installation
                    cmd = [
                        'mv',
                        f'{extract_path}/{full_version}',
                        os.path.join(
                            settings.RTD_DOCKER_WORKDIR,
                            f'.asdf/installs/{tool}/{full_version}',
                        ),
                    ]
                    self.build_env.run(
                        *cmd,
                        record=False,
                    )
            else:
                log.debug(
                    'Cached version for tool not found. os=%s tool=%s version=% filename=%s',
                    self.config.build.os,
                    tool,
                    full_version,
                    tool_path,
                )
                # If the tool version selected is not available from the
                # cache we compile it at build time
                cmd = [
                    # TODO: make ``PYTHON_CONFIGURE_OPTS="--enable-shared"``
                    # environment variable to work here. Note that
                    # ``self.build_env.run`` does not support passing
                    # environment for a particular command:
                    # https://github.com/readthedocs/readthedocs.org/blob/9d2d1a2/readthedocs/doc_builder/environments.py#L430-L431
                    'asdf',
                    'install',
                    tool,
                    full_version,
                ]
                self.build_env.run(
                    *cmd,
                )

            # Make the tool version chosen by the user the default one
            cmd = [
                'asdf',
                'global',
                tool,
                full_version,
            ]
            self.build_env.run(
                *cmd,
            )

            # Recreate shims for this tool to make the new version
            # installed available
            # https://asdf-vm.com/learn-more/faq.html#newly-installed-exectable-not-running
            cmd = [
                'asdf',
                'reshim',
                tool,
            ]
            self.build_env.run(
                *cmd,
                record=False,
            )

            if all([
                    tool == 'python',
                    # Do not install them if the tool version was cached
                    # because these dependencies are already installed when
                    # created with our script and uploaded to the cache's
                    # bucket
                    not tool_version_cached,
                    # Do not install them on conda/mamba since they are not
                    # needed because the environment is managed by conda/mamba
                    # itself
                    self.config.python_interpreter not in ('conda', 'mamba'),
            ]):
                # Install our own requirements if the version is compiled
                cmd = [
                    'python',
                    '-mpip',
                    'install',
                    '-U',
                    'virtualenv',
                    'setuptools',
                ]
                self.build_env.run(
                    *cmd,
                )

    def install_requirements(self):
        """Install all requirements from the config object."""
        for install in self.config.python.install:
            if isinstance(install, PythonInstallRequirements):
                self.install_requirements_file(install)
            if isinstance(install, PythonInstall):
                self.install_package(install)

    def install_package(self, install):
        """
        Install the package using pip or setuptools.

        :param install: A install object from the config module.
        :type install: readthedocs.config.models.PythonInstall
        """
        if install.method == PIP:
            # Prefix ./ so pip installs from a local path rather than pypi
            local_path = (
                os.path.join('.', install.path) if install.path != '.' else install.path
            )
            extra_req_param = ''
            if install.extra_requirements:
                extra_req_param = '[{}]'.format(
                    ','.join(install.extra_requirements)
                )
            self.build_env.run(
                self.venv_bin(filename='python'),
                '-m',
                'pip',
                'install',
                '--upgrade',
                '--upgrade-strategy',
                'eager',
                *self._pip_cache_cmd_argument(),
                '{path}{extra_requirements}'.format(
                    path=local_path,
                    extra_requirements=extra_req_param,
                ),
                cwd=self.checkout_path,
                bin_path=self.venv_bin(),
            )
        elif install.method == SETUPTOOLS:
            self.build_env.run(
                self.venv_bin(filename='python'),
                os.path.join(install.path, 'setup.py'),
                'install',
                '--force',
                cwd=self.checkout_path,
                bin_path=self.venv_bin(),
            )

    def _pip_cache_cmd_argument(self):
        """
        Return the pip command ``--cache-dir`` or ``--no-cache-dir`` argument.

        The decision is made considering if the directories are going to be
        cleaned after the build (``RTD_CLEAN_AFTER_BUILD=True`` or project has
        the ``CLEAN_AFTER_BUILD`` feature enabled) or project has the feature
        ``CACHED_ENVIRONMENT``. In this case, there is no need to cache
        anything.
        """
        if (
            # Cache is going to be removed anyways
            settings.RTD_CLEAN_AFTER_BUILD or
            self.project.has_feature(Feature.CLEAN_AFTER_BUILD) or
            # Cache will be pushed/pulled each time and won't be used because
            # packages are already installed in the environment
            self.project.has_feature(Feature.CACHED_ENVIRONMENT)
        ):
            return [
                '--no-cache-dir',
            ]
        return [
            '--cache-dir',
            self.project.pip_cache_path,
        ]

    def venv_bin(self, filename=None):
        """
        Return path to the virtualenv bin path, or a specific binary.

        :param filename: If specified, add this filename to the path return
        :returns: Path to virtualenv bin or filename in virtualenv bin
        """
        parts = [self.venv_path(), 'bin']
        if filename is not None:
            parts.append(filename)
        return os.path.join(*parts)

    def environment_json_path(self):
        """Return the path to the ``readthedocs-environment.json`` file."""
        return os.path.join(
            self.venv_path(),
            'readthedocs-environment.json',
        )

    @property
    def is_obsolete(self):
        """
        Determine if the environment is obsolete for different reasons.

        It checks the the data stored at ``readthedocs-environment.json`` and
        compares it with the one to be used. In particular:

        * the Python version (e.g. 2.7, 3, 3.6, etc)
        * the Docker image name
        * the Docker image hash
        * the environment variables hash

        :returns: ``True`` when it's obsolete and ``False`` otherwise

        :rtype: bool
        """
        # Always returns False if we don't have information about what Python
        # version/Docker image was used to create the venv as backward
        # compatibility.
        if not os.path.exists(self.environment_json_path()):
            return False

        try:
            with open(self.environment_json_path(), 'r') as fpath:
                environment_conf = json.load(fpath)
        except (IOError, TypeError, KeyError, ValueError):
            log.warning(
                'Unable to read/parse readthedocs-environment.json file',
            )
            # We remove the JSON file here to avoid cycling over time with a
            # corrupted file.
            os.remove(self.environment_json_path())
            return True

        env_python = environment_conf.get('python', {})
        env_build = environment_conf.get('build', {})
        env_vars_hash = environment_conf.get('env_vars_hash', None)

        # By defaulting non-existent options to ``None`` we force a wipe since
        # we don't know how the environment was created
        env_python_version = env_python.get('version', None)
        env_build_image = env_build.get('image', None)
        env_build_hash = env_build.get('hash', None)

        if isinstance(self.build_env, DockerBuildEnvironment):
            build_image = self.config.docker_image
            image_hash = self.build_env.image_hash
        else:
            # e.g. LocalBuildEnvironment
            build_image = None
            image_hash = None

        # If the user define the Python version just as a major version
        # (e.g. ``2`` or ``3``) we won't know exactly which exact version was
        # used to create the venv but we can still compare it against the new
        # one coming from the project version config.
        return any([
            env_python_version != self.config.python_full_version,
            env_build_image != build_image,
            env_build_hash != image_hash,
            env_vars_hash != self._get_env_vars_hash(),
        ])

    def _get_env_vars_hash(self):
        """
        Returns the sha256 hash of all the environment variables and their values.

        If there are no environment variables configured for the associated project,
        it returns sha256 hash of empty string.
        """
        m = hashlib.sha256()

        env_vars = self.version.project.environment_variables(
            public_only=self.version.is_external
        )

        for variable, value in env_vars.items():
            hash_str = f'_{variable}_{value}_'
            m.update(hash_str.encode('utf-8'))
        return m.hexdigest()

    def save_environment_json(self):
        """
        Save on builders disk data about the environment used to build docs.

        The data is saved as a ``.json`` file with this information on it:

        - python.version
        - build.image
        - build.hash
        - env_vars_hash
        """
        data = {
            'python': {
                'version': self.config.python_full_version,
            },
            'env_vars_hash': self._get_env_vars_hash(),
        }

        if isinstance(self.build_env, DockerBuildEnvironment):
            build_image = self.config.docker_image
            data.update({
                'build': {
                    'image': build_image,
                    'hash': self.build_env.image_hash,
                },
            })

        with open(self.environment_json_path(), 'w') as fpath:
            # Compatibility for Py2 and Py3. ``io.TextIOWrapper`` expects
            # unicode but ``json.dumps`` returns str in Py2.
            fpath.write(str(json.dumps(data)))


class Virtualenv(PythonEnvironment):

    """
    A virtualenv_ environment.

    .. _virtualenv: https://virtualenv.pypa.io/
    """

    def venv_path(self):
        return os.path.join(self.project.doc_path, 'envs', self.version.slug)

    def setup_base(self):
        """
        Create a virtualenv, invoking ``python -mvirtualenv``.

        .. note::

            ``--no-download`` was removed because of the pip breakage,
            it was sometimes installing pip 20.0 which broke everything
            https://github.com/readthedocs/readthedocs.org/issues/6585

            Important not to add empty string arguments, see:
            https://github.com/readthedocs/readthedocs.org/issues/7322
        """
        cli_args = [
            '-mvirtualenv',
        ]
        if self.config.python.use_system_site_packages:
            cli_args.append('--system-site-packages')

        # Append the positional destination argument
        cli_args.append(
            self.venv_path(),
        )

        self.build_env.run(
            self.config.python_interpreter,
            *cli_args,
            # Don't use virtualenv bin that doesn't exist yet
            bin_path=None,
            # Don't use the project's root, some config files can interfere
            cwd=None,
        )

    def install_core_requirements(self):
        """Install basic Read the Docs requirements into the virtualenv."""
        pip_install_cmd = [
            self.venv_bin(filename='python'),
            '-m',
            'pip',
            'install',
            '--upgrade',
            *self._pip_cache_cmd_argument(),
        ]

        # Install latest pip and setuptools first,
        # so it is used when installing the other requirements.
        pip_version = self.project.get_feature_value(
            Feature.DONT_INSTALL_LATEST_PIP,
            # 20.3 uses the new resolver by default.
            positive='pip<20.3',
            negative='pip',
        )
        cmd = pip_install_cmd + [pip_version, 'setuptools']
        self.build_env.run(
            *cmd, bin_path=self.venv_bin(), cwd=self.checkout_path
        )

        requirements = [
            'mock==1.0.1',
            'pillow==5.4.1',
            'alabaster>=0.7,<0.8,!=0.7.5',
            'commonmark==0.8.1',
            'recommonmark==0.5.0',
        ]

        if self.config.doctype == 'mkdocs':
            requirements.append(
                self.project.get_feature_value(
                    Feature.DEFAULT_TO_MKDOCS_0_17_3,
                    positive='mkdocs==0.17.3',
                    negative=self.project.get_feature_value(
                        Feature.USE_MKDOCS_LATEST,
                        positive='mkdocs<1.1',
                        negative='mkdocs',
                    ),
                ),
            )
        else:
            requirements.extend([
                self.project.get_feature_value(
                    Feature.USE_SPHINX_LATEST,
                    positive='sphinx',
                    negative='sphinx<2',
                ),
                # If defaulting to Sphinx 2+, we need to push the latest theme
                # release as well. `<0.5.0` is not compatible with Sphinx 2+
                self.project.get_feature_value(
                    Feature.USE_SPHINX_LATEST,
                    positive='sphinx-rtd-theme',
                    negative='sphinx-rtd-theme<0.5',
                ),
                self.project.get_feature_value(
                    Feature.USE_SPHINX_RTD_EXT_LATEST,
                    positive='readthedocs-sphinx-ext',
                    negative='readthedocs-sphinx-ext<2.2',
                ),
            ])

        cmd = copy.copy(pip_install_cmd)
        if self.config.python.use_system_site_packages:
            # Other code expects sphinx-build to be installed inside the
            # virtualenv.  Using the -I option makes sure it gets installed
            # even if it is already installed system-wide (and
            # --system-site-packages is used)
            cmd.append('-I')
        cmd.extend(requirements)
        self.build_env.run(
            *cmd,
            bin_path=self.venv_bin(),
            cwd=self.checkout_path  # noqa - no comma here in py27 :/
        )

    def install_requirements_file(self, install):
        """
        Install a requirements file using pip.

        :param install: A install object from the config module.
        :type install: readthedocs.config.models.PythonInstallRequirements
        """
        requirements_file_path = install.requirements
        if requirements_file_path is None:
            # This only happens when the config file is from v1.
            # We try to find a requirements file.
            builder_class = get_builder_class(self.config.doctype)
            docs_dir = (
                builder_class(
                    build_env=self.build_env,
                    python_env=self,
                ).docs_dir()
            )
            paths = [docs_dir, '']
            req_files = ['pip_requirements.txt', 'requirements.txt']
            for path, req_file in itertools.product(paths, req_files):
                test_path = os.path.join(self.checkout_path, path, req_file)
                if os.path.exists(test_path):
                    requirements_file_path = os.path.relpath(
                        test_path,
                        self.checkout_path,
                    )
                    break

        if requirements_file_path:
            args = [
                self.venv_bin(filename='python'),
                '-m',
                'pip',
                'install',
            ]
            if self.project.has_feature(Feature.PIP_ALWAYS_UPGRADE):
                args += ['--upgrade']
            args += [
                '--exists-action=w',
                *self._pip_cache_cmd_argument(),
                '-r',
                requirements_file_path,
            ]
            self.build_env.run(
                *args,
                cwd=self.checkout_path,
                bin_path=self.venv_bin(),
            )

    def list_packages_installed(self):
        """List packages installed in pip."""
        args = [
            self.venv_bin(filename='python'),
            '-m',
            'pip',
            'list',
            # Include pre-release versions.
            '--pre',
        ]
        self.build_env.run(
            *args,
            cwd=self.checkout_path,
            bin_path=self.venv_bin(),
        )


class Conda(PythonEnvironment):

    """
    A Conda_ environment.

    .. _Conda: https://conda.io/docs/
    """

    def venv_path(self):
        return os.path.join(self.project.doc_path, 'conda', self.version.slug)

    def conda_bin_name(self):
        """
        Decide whether use ``mamba`` or ``conda`` to create the environment.

        Return ``mamba`` if the project has ``CONDA_USES_MAMBA`` feature and
        ``conda`` otherwise. This will be the executable name to be used when
        creating the conda environment.

        ``mamba`` is really fast to solve dependencies and download channel
        metadata on startup.

        See https://github.com/QuantStack/mamba
        """
        # Config file using ``build.tools.python``
        if self.config.using_build_tools:
            return self.config.python_interpreter

        # Config file using ``conda``
        if self.project.has_feature(Feature.CONDA_USES_MAMBA):
            return 'mamba'
        return 'conda'

    def _update_conda_startup(self):
        """
        Update ``conda`` before use it for the first time.

        This makes the Docker image to use the latest version of ``conda``
        independently the version of Miniconda that it has installed.
        """
        self.build_env.run(
            # TODO: use ``self.conda_bin_name()`` once ``mamba`` is installed in
            # the Docker image
            'conda',
            'update',
            '--yes',
            '--quiet',
            '--name=base',
            '--channel=defaults',
            'conda',
            cwd=self.checkout_path,
        )

    def _install_mamba(self):
        self.build_env.run(
            'conda',
            'install',
            '--yes',
            '--quiet',
            '--name=base',
            '--channel=conda-forge',
            'python=3.7',
            'mamba',
            cwd=self.checkout_path,
        )

    def setup_base(self):
        conda_env_path = os.path.join(self.project.doc_path, 'conda')
        version_path = os.path.join(conda_env_path, self.version.slug)

        if os.path.exists(version_path):
            # Re-create conda directory each time to keep fresh state
            log.info(
                LOG_TEMPLATE,
                {
                    'project': self.project.slug,
                    'version': self.version.slug,
                    'msg': 'Removing existing conda directory',
                },
            )
            shutil.rmtree(version_path)

        if self.project.has_feature(Feature.UPDATE_CONDA_STARTUP):
            self._update_conda_startup()

        if self.project.has_feature(Feature.CONDA_APPEND_CORE_REQUIREMENTS):
            self._append_core_requirements()
            self._show_environment_yaml()

        if all([
                # The project has CONDA_USES_MAMBA feature enabled and,
                self.project.has_feature(Feature.CONDA_USES_MAMBA),
                # the project is not using ``build.tools``,
                # which has mamba installed via asdf.
                not self.config.using_build_tools,
        ]):
            self._install_mamba()

        self.build_env.run(
            self.conda_bin_name(),
            'env',
            'create',
            '--quiet',
            '--name',
            self.version.slug,
            '--file',
            self.config.conda.environment,
            bin_path=None,  # Don't use conda bin that doesn't exist yet
            cwd=self.checkout_path,
        )

    def _show_environment_yaml(self):
        """Show ``environment.yml`` file in the Build output."""
        self.build_env.run(
            'cat',
            self.config.conda.environment,
            cwd=self.checkout_path,
        )

    def _append_core_requirements(self):
        """
        Append Read the Docs dependencies to Conda environment file.

        This help users to pin their dependencies properly without us upgrading
        them in the second ``conda install`` run.

        See https://github.com/readthedocs/readthedocs.org/pull/5631
        """
        try:
            inputfile = codecs.open(
                os.path.join(
                    self.checkout_path,
                    self.config.conda.environment,
                ),
                encoding='utf-8',
                mode='r',
            )
            environment = parse_yaml(inputfile)
        except IOError:
            log.warning(
                'There was an error while reading Conda environment file.',
            )
        except ParseError:
            log.warning(
                'There was an error while parsing Conda environment file.',
            )
        else:
            # Append conda dependencies directly to ``dependencies`` and pip
            # dependencies to ``dependencies.pip``
            pip_requirements, conda_requirements = self._get_core_requirements()
            dependencies = environment.get('dependencies', [])
            pip_dependencies = {'pip': pip_requirements}

            for item in dependencies:
                if isinstance(item, dict) and 'pip' in item:
                    # NOTE: pip can be ``None``
                    pip_requirements.extend(item.get('pip') or [])
                    dependencies.remove(item)
                    break

            dependencies.append(pip_dependencies)
            dependencies.extend(conda_requirements)
            environment.update({'dependencies': dependencies})
            try:
                outputfile = codecs.open(
                    os.path.join(
                        self.checkout_path,
                        self.config.conda.environment,
                    ),
                    encoding='utf-8',
                    mode='w',
                )
                yaml.safe_dump(environment, outputfile)
            except IOError:
                log.warning(
                    'There was an error while writing the new Conda '
                    'environment file.',
                )

    def _get_core_requirements(self):
        # Use conda for requirements it packages
        conda_requirements = [
            'mock',
            'pillow',
        ]

        if self.project.has_feature(Feature.CONDA_USES_MAMBA):
            conda_requirements.append('pip')

        # Install pip-only things.
        pip_requirements = [
            'recommonmark',
        ]

        if self.config.doctype == 'mkdocs':
            pip_requirements.append('mkdocs')
        else:
            pip_requirements.append('readthedocs-sphinx-ext')
            conda_requirements.extend(['sphinx', 'sphinx_rtd_theme'])

        return pip_requirements, conda_requirements

    def install_core_requirements(self):
        """Install basic Read the Docs requirements into the Conda env."""

        if self.project.has_feature(Feature.CONDA_APPEND_CORE_REQUIREMENTS):
            # Skip install core requirements since they were already appended to
            # the user's ``environment.yml`` and installed at ``conda env
            # create`` step.
            return

        pip_requirements, conda_requirements = self._get_core_requirements()
        # Install requirements via ``conda install`` command if they were
        # not appended to the ``environment.yml`` file.
        cmd = [
            self.conda_bin_name(),
            'install',
            '--yes',
            '--quiet',
            '--name',
            self.version.slug,
        ]
        cmd.extend(conda_requirements)
        self.build_env.run(
            *cmd,
            cwd=self.checkout_path,
        )

        # Install requirements via ``pip install``
        pip_cmd = [
            self.venv_bin(filename='python'),
            '-m',
            'pip',
            'install',
            '-U',
            *self._pip_cache_cmd_argument(),
        ]
        pip_cmd.extend(pip_requirements)
        self.build_env.run(
            *pip_cmd,
            bin_path=self.venv_bin(),
            cwd=self.checkout_path  # noqa - no comma here in py27 :/
        )

    def install_requirements_file(self, install):
        # as the conda environment was created by using the ``environment.yml``
        # defined by the user, there is nothing to update at this point
        pass

    def list_packages_installed(self):
        """List packages installed in conda."""
        args = [
            self.conda_bin_name(),
            'list',
            '--name',
            self.version.slug,
        ]
        self.build_env.run(
            *args,
            cwd=self.checkout_path,
            bin_path=self.venv_bin(),
        )
