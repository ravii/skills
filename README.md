# 个人 AI Skills

用于归档个人工作中创建和维护的 AI skills，按领域和用途独立组织。

## 目录结构

```text
.
├── skills/
│   └── <skill-name>/
│       ├── SKILL.md       # skill 的名称、用途和执行说明
│       ├── references/    # 按需添加的参考资料
│       ├── scripts/       # 按需添加的辅助脚本
│       └── assets/        # 按需添加的模板或其他资源
└── templates/
    └── skill/
        └── SKILL.md       # 新 skill 的起始模板
```

## 新增 skill

1. 从 `templates/skill/` 复制一份目录到 `skills/`，并使用简短、稳定的英文目录名。
2. 填写 `SKILL.md`，明确说明适用场景、输入、输出和执行步骤。
3. 只将该 skill 需要的参考资料、脚本和资源放入对应目录。

每个 skill 应当能够独立理解和使用，避免依赖仓库外未记录的上下文。
