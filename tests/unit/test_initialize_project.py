from cfengine_cli import initialize_project


def test_policy_module_main_bundle_is_namespaced():
    main_cf = initialize_project._main_cf("My-Module", "my_module")
    assert 'namespace => "my_module";' in main_cf
    assert '"my_module:main";' in main_cf

    module = initialize_project._module_definition("my-module", "my_module", False)
    assert "bundles my_module:main" in module["steps"]
    assert "bundles main" not in module["steps"]
