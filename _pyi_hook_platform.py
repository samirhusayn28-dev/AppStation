# -*- mode: python -*-
# PyInstaller hook to include the `platform` module
import PyInstaller.utils.hooks as hooks

def get_HiddenImports():
    # The platform module is usually included automatically, but sometimes needs help on macOS
    modules = ['platform']
    additional_imports = []
    
    # Collect all submodules that might be needed
    import platform as plat_mod
    for attr in dir(plat_mod):
        sub = getattr(plat_mod, attr)
        if hasattr(sub, '__file__') and '__py_' in str(sub.__file__) or '.py' in str(sub.__file__):
            mod_name = attr
            if mod_name not in modules:
                modules.append(mod_name)
    
    return modules
