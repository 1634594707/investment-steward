# Core Schemas

JSON Schema 是跨语言公共契约的发布格式。Python Pydantic 模型与 TypeScript 类型必须与这里的 `schema_version` 对齐；新增字段必须保持向后兼容，改变字段语义必须升级主版本并新增 ADR。

v0 冻结四个基础契约：InvestmentPolicyVersion、Evidence、PluginCapability、AgentResponse。
