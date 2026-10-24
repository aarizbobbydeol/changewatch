"""Changed files and added line ranges from real git diff output."""
from app.diffparse import parse_unified_diff

DIFF = '''diff --git a/app/billing.py b/app/billing.py
index 1111111..2222222 100644
--- a/app/billing.py
+++ b/app/billing.py
@@ -40,3 +40,4 @@ def total_cents(order):
     if not order.items:
         return 0
-    return sum(i.price for i in order.items)
+    total = sum(i.price for i in order.items)
+    return total / order.discount
@@ -90,2 +91,3 @@ def refund(order):
     log("refund")
++++ a line whose content starts with three plus signs
     return 0
diff --git a/app/new.py b/app/new.py
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/app/new.py
@@ -0,0 +1,2 @@
+def hello():
+    return 1
\\ No newline at end of file
diff --git a/old/name.py b/app/renamed.py
similarity index 90%
rename from old/name.py
rename to app/renamed.py
index 4444444..5555555 100644
--- a/old/name.py
+++ b/app/renamed.py
@@ -1 +1 @@
-x = 1
+x = 2
diff --git a/app/gone.py b/app/gone.py
deleted file mode 100644
index 6666666..0000000
--- a/app/gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-a = 1
-b = 2
'''


def test_parses_paths_statuses_and_new_side_line_ranges():
    files = {f["path"]: f for f in parse_unified_diff(DIFF)}
    assert files["app/billing.py"]["status"] == "modified"
    assert files["app/billing.py"]["added_ranges"] == [[42, 43], [92, 92]]
    assert files["app/new.py"] == {"path": "app/new.py", "status": "added", "added_ranges": [[1, 2]]}
    assert files["app/renamed.py"]["status"] == "renamed" and files["app/renamed.py"]["added_ranges"] == [[1, 1]]
    assert files["app/gone.py"]["status"] == "deleted" and files["app/gone.py"]["added_ranges"] == []


def test_git_show_header_and_garbage_are_ignored():
    assert parse_unified_diff("commit abc\nAuthor: x\n\n    message\n") == []
    assert parse_unified_diff("") == []
    shown = "commit abc\nAuthor: a <a@b.c>\n\n    fix\n\n" + DIFF
    assert [f["path"] for f in parse_unified_diff(shown)] == ["app/billing.py", "app/new.py", "app/renamed.py", "app/gone.py"]
