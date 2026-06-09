from github_monitor import extract_file_dependencies, extract_imports_from_source


def test_extract_imports_from_go_source_handles_import_blocks():
    source = """
package main

import (
    "fmt"
    httpClient "net/http"
    . "github.com/sirupsen/logrus"
    "github.com/acme/project/pkg/foo"
)
"""

    dependencies = extract_imports_from_source("cmd/app/main.go", source)
    targets = {target for _, _, target in dependencies}

    assert "fmt" in targets
    assert "net/http" in targets
    assert "github.com/sirupsen/logrus" in targets
    assert "github.com/acme/project/pkg/foo" in targets


def test_extract_imports_from_go_mod_handles_require_blocks():
    source = """
module github.com/acme/project

go 1.22

require (
    github.com/gin-gonic/gin v1.9.1
    golang.org/x/net v0.27.0 // indirect
)

require github.com/stretchr/testify v1.8.4
"""

    dependencies = extract_imports_from_source("go.mod", source)
    targets = {target for _, _, target in dependencies}

    assert "github.com/gin-gonic/gin" in targets
    assert "golang.org/x/net" in targets
    assert "github.com/stretchr/testify" in targets


def test_extract_imports_from_go_work_handles_use_and_replace_blocks():
    source = """
go 1.22

use (
    ./services/api
    ./libs/shared
)

replace (
    github.com/acme/oldmod => github.com/acme/newmod v1.2.3
    github.com/acme/local => ../local/module
)
"""

    dependencies = extract_imports_from_source("go.work", source)
    targets = {target for _, _, target in dependencies}

    assert "./services/api" in targets
    assert "./libs/shared" in targets
    assert "github.com/acme/oldmod" in targets
    assert "github.com/acme/newmod" in targets
    assert "github.com/acme/local" in targets
    assert "../local/module" in targets


def test_extract_file_dependencies_handles_go_patch_lines():
    compact_files = [
        {
            "filename": "cmd/app/main.go",
            "patch": """@@
+import (
+    "net/http"
+)
""",
        },
        {
            "filename": "go.mod",
            "patch": """@@
+require (
+    github.com/google/uuid v1.6.0
+)
""",
        },
        {
            "filename": "go.work",
            "patch": """@@
+use (
+    ./services/api
+)
+
+replace (
+    github.com/acme/oldmod => github.com/acme/newmod v1.2.3
+)
""",
        },
    ]

    dependencies = extract_file_dependencies(compact_files)
    targets = {target for _, _, target in dependencies}

    assert "net/http" in targets
    assert "github.com/google/uuid" in targets
    assert "./services/api" in targets
    assert "github.com/acme/oldmod" in targets
    assert "github.com/acme/newmod" in targets
