# Estructura de Archivos Estáticos - Landing DOMINIO

## Organización de carpetas

### CSS
```
css/
├── style.css              # CSS principal que importa todo
├── base/
│   ├── reset.css         # Reset de estilos del navegador
│   ├── variables.css     # Variables CSS (colores, fuentes, etc.)
│   └── typography.css    # Estilos de tipografía
└── components/
    ├── navbar.css        # Estilos del navbar
    ├── hero.css          # Estilos de la sección hero
    ├── cards.css         # Estilos de tarjetas de productos
    ├── forms.css         # Estilos de formularios
    └── footer.css        # Estilos del footer
```

### JavaScript
```
js/
├── main.js               # JavaScript principal
├── components/
│   ├── navbar.js        # Funcionalidad del navbar (menú móvil, scroll)
│   ├── slider.js        # Carrusel/slider de productos
│   └── form.js          # Validación de formularios
└── utils/
    ├── helpers.js       # Funciones helper reutilizables
    └── animations.js    # Animaciones y efectos
```

### Imágenes
```
images/
├── logo/
│   ├── logo.png         # Logo principal (fondo oscuro)
│   ├── logo-white.png   # Logo blanco (fondo claro)
│   └── favicon.ico      # Favicon del sitio
├── hero/
│   ├── hero-bg.jpg      # Imagen de fondo del hero
│   └── hero-image.png   # Imagen principal del hero
├── productos/
│   ├── producto-1.jpg   # Imagen del producto 1
│   ├── producto-2.jpg   # Imagen del producto 2
│   └── producto-3.jpg   # Imagen del producto 3
├── team/
│   └── team-photo.jpg   # Foto del equipo
└── icons/
    ├── icon-1.svg       # Iconos en formato SVG
    └── icon-2.svg
```

## Cómo usar en templates

### Cargar imágenes
```django
{% load static %}
<img src="{% static 'landing/images/logo/logo.png' %}" alt="Logo">
```

### Cargar CSS
```django
<link rel="stylesheet" href="{% static 'landing/css/style.css' %}">
```

### Cargar JavaScript
```django
<script src="{% static 'landing/js/main.js' %}"></script>
```

## Recomendaciones

1. **Tamaños de imágenes:**
   - Logo: máximo 200x200px, formato PNG con transparencia
   - Hero background: 1920x1080px, formato JPG optimizado
   - Productos: 800x800px, formato JPG o PNG
   - Iconos: formato SVG cuando sea posible

2. **Optimización:**
   - Comprimir todas las imágenes antes de subirlas
   - Usar formatos modernos como WebP para mejor rendimiento
   - Minificar CSS y JS en producción

3. **Nombres de archivos:**
   - Usar minúsculas y guiones (kebab-case)
   - Nombres descriptivos
   - Ejemplo: `producto-principal.jpg`, `logo-white.png`
