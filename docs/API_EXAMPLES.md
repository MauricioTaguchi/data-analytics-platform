# Exemplos da API

## Registro

```bash
curl -X POST http://localhost:8000/api/v1/auth/register   -H "Content-Type: application/json"   -d '{"name":"Mauricio","email":"mauricio@example.com","password":"senha12345"}'
```

## Create project

```bash
curl -X POST http://localhost:8000/api/v1/projects   -H "Authorization: Bearer SEU_TOKEN"   -H "Content-Type: application/json"   -d '{"name":"Análise de Vendas","description":"Indicadores comerciais"}'
```

## Upload

```bash
curl -X POST http://localhost:8000/api/v1/datasets/project/1   -H "Authorization: Bearer SEU_TOKEN"   -F "file=@vendas.csv"
```
