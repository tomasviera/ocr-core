# Guía git del core (corta)

El core (`E:\ocr-core`) es el **único** repo git de todo el esquema. Los proyectos
(prensa, manuscritos-v2) **no** son repos git: solo vendorizan copias fijadas.

---

## 0. Identidad (una sola vez, global)

Si `git config --global user.name` está vacío, seteala (sin esto, `git commit` y
`bump_core.ps1` fallan):

```powershell
git config --global user.name  "Tomás"
git config --global user.email "adolasi@gmail.com"
```

## 1. Inicializar y primer commit (Fase 0)

```powershell
cd E:\ocr-core
git init -b main
git add -A
git commit -m "core v0: scaffold (estructura + contratos + scripts)"
```

## 2. Crear el repo GitHub privado y pushear

`gh` CLI no está instalado. Dos opciones:

**A) Por web (sin instalar nada):** crear un repo **privado** vacío en
<https://github.com/new> (nombre sugerido: `ocr-core`; sin README/.gitignore/licencia,
porque ya los tenemos). Después:

```powershell
cd E:\ocr-core
git remote add origin https://github.com/<usuario>/ocr-core.git
git push -u origin main
```

**B) Con gh CLI** (si lo instalás, `winget install GitHub.cli`):

```powershell
cd E:\ocr-core
gh auth login
gh repo create ocr-core --private --source . --remote origin --push
```

## 3. Publicar una versión nueva del core

Desde `E:\ocr-core`, cuando entre código nuevo (ej. el motor agy en Fase 1):

```powershell
.\bump_core.ps1 -Version v1 -Message "motor agy"
```

Escribe `VERSION`, commitea, crea el tag `v1` y lo pushea (rama + tag).

## 4. Vendorizar el core en un proyecto

`actualizar_core.ps1` es la plantilla a copiar en la raíz de cada proyecto consumidor.
Desde la raíz del proyecto:

```powershell
.\actualizar_core.ps1 -Version v1
```

Copia el árbol del tag a `core_vendor\`, inyecta el header `GENERADO`, escribe
`core_version.txt`. El vendor anterior queda en `core_vendor.prev`.

## 5. Ver historial / volver atrás

```powershell
git log --oneline --decorate     # commits y tags
git tag                          # versiones publicadas
git show v1:motores/lib_agy.php  # ver un archivo en una versión
```

**Rollback de un proyecto** (sin tocar el otro): vendorizar un tag anterior.

```powershell
# en la raíz del proyecto
.\actualizar_core.ps1 -Version v0
```

El estado del proyecto = lo que diga su `core_version.txt`. Cada proyecto puede
quedar en una versión distinta del core; son independientes.
