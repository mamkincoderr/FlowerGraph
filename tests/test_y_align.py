import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
import numpy as np
import inspect
import ui.plot_area as pa
import ui.main_window as mw

# Проверяем новые методы PlotArea
for m in ['y_align_overlay','y_align_distribute','y_align_auto',
          'y_align_grids','y_zoom_all','y_reset','clear_everything',
          '_get_channel_amplitudes']:
    assert hasattr(pa.PlotArea, m), f'Missing: {m}'
print('PlotArea Y-methods: OK')

# Проверяем новые методы MainWindow
for m in ['_on_new_file','_on_y_overlay','_on_y_distribute',
          '_on_y_auto','_on_y_grids','_on_y_reset',
          '_on_y_zoom_in','_on_y_zoom_out','_apply_y_results']:
    assert hasattr(mw.MainWindow, m), f'Missing: {m}'
print('MainWindow Y-methods: OK')

# Тест алгоритмов выравнивания без Qt
from core.ring_buffer import RingBuffer

buf = RingBuffer(3, 1000)
t = np.linspace(0, 1, 500, dtype=np.float64)
v = np.column_stack([
    np.sin(2*np.pi*10*t),
    0.3 * np.sin(2*np.pi*5*t),
    2.0 * np.cos(2*np.pi*20*t),
]).astype(np.float32)
buf.push(t, v)

_, v_last = buf.get_last(500)
amps = np.max(np.abs(v_last), axis=0)
print(f'Amps: CH1={amps[0]:.3f}  CH2={amps[1]:.3f}  CH3={amps[2]:.3f}')
assert abs(amps[0] - 1.0) < 0.01
assert abs(amps[1] - 0.3) < 0.01
assert abs(amps[2] - 2.0) < 0.01

# distribute
g = float(np.max(amps))
s = 1.0 / g
n = 3
spacing = 2.2
offsets = [((n-1)/2.0 - i) * spacing for i in range(n)]
print(f'Distribute scale={s:.3f}  offsets={[f"{o:.2f}" for o in offsets]}')
assert offsets[0] > offsets[1] > offsets[2]

# auto
scales_auto = [1.0/a for a in amps]
print(f'Auto scales: CH1={scales_auto[0]:.2f}  CH2={scales_auto[1]:.2f}  CH3={scales_auto[2]:.2f}')
assert scales_auto[2] < scales_auto[0] < scales_auto[1]

print('ALL OK')
