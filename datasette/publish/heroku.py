from contextlib import contextmanager
from datasette import hookimpl
import click
import json
import os
import shlex
from subprocess import call, check_output
import tempfile

from .common import (
    add_common_publish_arguments_and_options,
    fail_if_publish_binary_not_installed,
)
from datasette.utils import link_or_copy, link_or_copy_directory, parse_metadata


@hookimpl
def publish_subcommand(publish):
    @publish.command()
    @add_common_publish_arguments_and_options
    @click.option(
        "-n",
        "--name",
        default="datasette",
        help="Application name to use when deploying",
    )
    def heroku(
        files,
        metadata,
        extra_options,
        branch,
        template_dir,
        plugins_dir,
        static,
        install,
        plugin_secret,
        version_note,
        secret,
        title,
        license,
        license_url,
        source,
        source_url,
        about,
        about_url,
        name,
    ):
        fail_if_publish_binary_not_installed(
            "heroku", "Heroku", "https://cli.heroku.com"
        )

        # Check for heroku-builds plugin
        plugins = [
            line.split()[0] for line in check_output(["heroku", "plugins"], shell=True).splitlines()
        ]
        if b"heroku-builds" not in plugins:
            click.echo(
                "Publishing to Heroku requires the heroku-builds plugin to be installed."
            )
            click.confirm(
                "Install it? (this will run `heroku plugins:install heroku-builds`)",
                abort=True,
            )
            call(["heroku", "plugins:install", "heroku-builds"], shell=True)

        extra_metadata = {
            "title": title,
            "license": license,
            "license_url": license_url,
            "source": source,
            "source_url": source_url,
            "about": about,
            "about_url": about_url,
        }

        environment_variables = {
            # Avoid uvicorn error: https://github.com/simonw/datasette/issues/633
            "WEB_CONCURRENCY": "1"
        }
        if plugin_secret:
            extra_metadata["plugins"] = {}
            for plugin_name, plugin_setting, setting_value in plugin_secret:
                environment_variable = (
                    "{}_{}".format(plugin_name, plugin_setting)
                    .upper()
                    .replace("-", "_")
                )
                environment_variables[environment_variable] = setting_value
                extra_metadata["plugins"].setdefault(plugin_name, {})[
                    plugin_setting
                ] = {"$env": environment_variable}

        with temporary_heroku_directory(
            files,
            name,
            metadata,
            extra_options,
            branch,
            template_dir,
            plugins_dir,
            static,
            install,
            version_note,
            secret,
            extra_metadata,
        ):
            app_name = None
            if name:
                # Check to see if this app already exists
                list_output = check_output(["heroku", "apps:list", "--json"], shell=True).decode(
                    "utf8"
                )
                apps = json.loads(list_output)

                for app in apps:
                    if app["name"] == name:
                        app_name = name
                        break

            if not app_name:
                # Create a new app
                cmd = ["heroku", "apps:create"]
                if name:
                    cmd.append(name)
                cmd.append("--json")
                create_output = check_output(cmd, shell=True).decode("utf8")
                app_name = json.loads(create_output)["name"]

            for key, value in environment_variables.items():
                call(
                    ["heroku", "config:set", "-a", app_name, "{}={}".format(key, value)], shell=True
                )

            call(["heroku", "builds:create", "-a", app_name, "--include-vcs-ignore"], shell=True)


@contextmanager
def temporary_heroku_directory(
    files,
    name,
    metadata,
    extra_options,
    branch,
    template_dir,
    plugins_dir,
    static,
    install,
    version_note,
    secret,
    extra_metadata=None,
):
    extra_metadata = extra_metadata or {}
    tmp = tempfile.TemporaryDirectory()
    saved_cwd = os.getcwd()

    file_paths = [os.path.join(saved_cwd, file_path) for file_path in files]
    file_names = [os.path.split(f)[-1] for f in files]

    if metadata:
        metadata_content = parse_metadata(metadata.read())
    else:
        metadata_content = {}
    for key, value in extra_metadata.items():
        if value:
            metadata_content[key] = value

    try:
        os.chdir(tmp.name)

        if metadata_content:
            open("metadata.json", "w").write(json.dumps(metadata_content, indent=2))

        open("runtime.txt", "w").write("python-3.8.5")

        if branch:
            install = [
                "https://github.com/simonw/datasette/archive/{branch}.zip".format(
                    branch=branch
                )
            ] + list(install)
        else:
            install = ["datasette"] + list(install)

        open("requirements.txt", "w").write("\n".join(install))
        os.mkdir("bin")
        open("bin/post_compile", "w").write(
            "datasette inspect --inspect-file inspect-data.json"
        )

        extras = []
        if template_dir:
            link_or_copy_directory(
                os.path.join(saved_cwd, template_dir),
                os.path.join(tmp.name, "templates"),
            )
            extras.extend(["--template-dir", "templates/"])
        if plugins_dir:
            link_or_copy_directory(
                os.path.join(saved_cwd, plugins_dir), os.path.join(tmp.name, "plugins")
            )
            extras.extend(["--plugins-dir", "plugins/"])
        if version_note:
            extras.extend(["--version-note", version_note])
        if metadata_content:
            extras.extend(["--metadata", "metadata.json"])
        if extra_options:
            extras.extend(extra_options.split())
        for mount_point, path in static:
            link_or_copy_directory(
                os.path.join(saved_cwd, path), os.path.join(tmp.name, mount_point)
            )
            extras.extend(["--static", "{}:{}".format(mount_point, mount_point)])

        quoted_files = " ".join(
            ["-i {}".format(shlex.quote(file_name)) for file_name in file_names]
        )
        procfile_cmd = "web: datasette serve --host 0.0.0.0 {quoted_files} --cors --port $PORT --inspect-file inspect-data.json {extras}".format(
            quoted_files=quoted_files, extras=" ".join(extras)
        )
        open("Procfile", "w").write(procfile_cmd)

        for path, filename in zip(file_paths, file_names):
            link_or_copy(path, os.path.join(tmp.name, filename))

        yield

    finally:
        tmp.cleanup()
        os.chdir(saved_cwd)
