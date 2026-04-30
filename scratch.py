import re

with open('website/index.html', 'r') as f:
    content = f.read()

# 1. Wider space
content = content.replace('max-width: 640px;', 'max-width: 960px;')

# 2. Reorder panels
# We want to move vtracerPanel to be right after bgPanel.
# Let's extract the blocks.
bg_idx = content.find('<!-- ★ Background removal panel -->')
size_idx = content.find('<!-- ★ Size / hoop panel -->')
palette_idx = content.find('<!-- ★ Colour palette panel -->')
vtracer_idx = content.find('<!-- ① vtracer params (raster only) -->')
vp3_idx = content.find('<!-- ② VP3 stitch params (always shown when file selected) -->')

# Extract vtracer block
vtracer_end_idx = vp3_idx
vtracer_block = content[vtracer_idx:vtracer_end_idx]

# Extract size and palette blocks
size_palette_block = content[size_idx:vtracer_idx]

# Build new content for that section
new_section = vtracer_block + size_palette_block

# Replace the section
content = content[:size_idx] + new_section + content[vtracer_end_idx:]

# 3. Change handleFileSelected
old_js1 = """                vtracerPanel.style.display = 'block';
                vp3Panel.style.display = 'block';
                bgPanel.style.display = 'block';
                sizePanel.style.display = 'block';
                palettePanel.style.display = 'block';
                // ensure all panels open
                [vtracerToggle, vp3Toggle, bgToggle, sizeToggle, paletteToggle].forEach(t => { t.classList.remove('collapsed'); });
                [vtracerBody, vp3Body, bgBody, sizeBody, paletteBody].forEach(b => { b.style.display = 'grid'; });"""

new_js1 = """                vtracerPanel.style.display = 'block';
                bgPanel.style.display = 'block';
                vp3Panel.style.display = 'none';
                sizePanel.style.display = 'none';
                palettePanel.style.display = 'none';
                // ensure all panels open
                [vtracerToggle, bgToggle].forEach(t => { t.classList.remove('collapsed'); });
                [vtracerBody, bgBody].forEach(b => { b.style.display = 'grid'; });"""

content = content.replace(old_js1, new_js1)

# 4. Change traceBtn handler
old_js2 = """                // Load traced SVG into the inline editor and reveal convert button
                loadSvgIntoEditor(svgText);
                convertBtn.style.display = 'inline-block'; convertBtn.disabled = false;
                showSuccess('Image traced to SVG — edit in the editor below, then click "Convert to VP3".');"""

new_js2 = """                // Load traced SVG into the inline editor and reveal convert button
                loadSvgIntoEditor(svgText);
                convertBtn.style.display = 'inline-block'; convertBtn.disabled = false;
                
                // Show VP3 conversion panels
                vp3Panel.style.display = 'block';
                sizePanel.style.display = 'block';
                palettePanel.style.display = 'block';
                [vp3Toggle, sizeToggle, paletteToggle].forEach(t => { t.classList.remove('collapsed'); });
                [vp3Body, sizeBody, paletteBody].forEach(b => { b.style.display = 'grid'; });

                showSuccess('Image traced to SVG — edit in the editor below, then click "Convert to VP3".');"""

content = content.replace(old_js2, new_js2)

with open('website/index.html', 'w') as f:
    f.write(content)

print("Done")
