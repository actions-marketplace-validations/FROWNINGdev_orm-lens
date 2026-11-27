# 规则参考

本节收录 ORM Lens 规则参考中已有的简体中文翻译。每个 `DOL` 编号都对应一项由 VS Code 扩展在编写代码时运行的静态检查。

尚未翻译的规则请参阅[完整英文规则参考](../../../rules/README.md)。

## Queryset 规则

| 代码 | 规则 | 类别 | 默认严重程度 | 适用性 |
|---|---|---|---|---|
| [DOL001](DOL001.md) | 优先使用 `.exists()`，而不是 `.count() > 0` | queryset | info | safe |
| [DOL002](DOL002.md) | 优先使用 `not .exists()`，而不是 `.count() == 0` | queryset | info | safe |
| [DOL003](DOL003.md) | 优先使用 `not .exists()`，而不是 `.first() is None` | queryset | info | safe |
| [DOL004](DOL004.md) | 优先使用 `.exists()`，而不是 `.first() is not None` | queryset | info | safe |
| [DOL005](DOL005.md) | 考虑用 `Q(...)` 代替 `.filter().exclude()` 链式调用 | queryset | hint | suggestion |
| [DOL006](DOL006.md) | 去掉 `for` 循环中包裹 QuerySet 的 `list()` | queryset | info | suggestion |
| [DOL007](DOL007.md) | 可能的 N+1：在 `for` 循环中访问属性 | queryset | warning | unsafe |

## 日期时间规则

| 代码 | 规则 | 类别 | 默认严重程度 | 适用性 |
|---|---|---|---|---|
| [DOL021](DOL021.md) | 应使用 `timezone.now()`，而不是 `datetime.now()` | datetime | warning | suggestion |
| [DOL022](DOL022.md) | `datetime.utcnow()` 已被弃用 | datetime | warning | suggestion |
