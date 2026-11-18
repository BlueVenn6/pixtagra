import importlib.util
import os
from pathlib import Path
import tempfile


def load_app_module():
    path = Path(__file__).resolve().parent / "pixtagra.py"
    spec = importlib.util.spec_from_file_location("pixtagra_gui_smoke", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    with tempfile.TemporaryDirectory(prefix="pixtagra_gui_smoke_") as temp_dir:
        home_dir = Path(temp_dir) / "home"
        config_dir = Path(temp_dir) / "config"
        home_dir.mkdir()
        os.environ["PSS_NO_STARTUP_DIALOG"] = "1"
        os.environ["PSS_CONFIG_DIR"] = str(config_dir)
        os.environ["PSS_CONFIG_FILE"] = str(config_dir / "config.json")
        os.environ["HOME"] = str(home_dir)
        os.environ["USERPROFILE"] = str(home_dir)

        module = load_app_module()
        app = None
        try:
            app = module.App()
            app.update_idletasks()
            app.update()

            assert app.winfo_exists() == 1
            assert app.title() == "PixTagra"
            assert hasattr(app, "welcome") and app.welcome.winfo_exists() == 1
            assert hasattr(app, "workspace") and app.workspace.winfo_exists() == 1
            assert hasattr(app, "model_var")
            assert hasattr(app, "key_entry")

            print("GUI smoke OK")
            print(f"title={app.title()}")
            print(f"welcome_exists={app.welcome.winfo_exists()}")
            print(f"workspace_exists={app.workspace.winfo_exists()}")
        finally:
            if app is not None:
                app.destroy()


if __name__ == "__main__":
    main()
