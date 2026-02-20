"""Live move-overhead graph for Tilted Variants Client.

Launched as a subprocess by variants_client.py so that tk.Tk() runs on this
process's main thread (required on Windows).

Usage: python ping_graph.py <data_file>

<data_file> is a JSON file written by the client after every engine move,
containing a list of [detect_ms, engine_ms, exec_ms, overhead_ms] tuples.
"""
import json
import pathlib
import sys
import tkinter as tk
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure


def main():
    if len(sys.argv) < 2:
        print("Usage: ping_graph.py <data_file>")
        sys.exit(1)

    data_file = pathlib.Path(sys.argv[1])

    BG       = '#1e1e2e'
    C_DETECT = '#4c8eda'   # blue  — detect segment
    C_EXEC   = '#e07060'   # coral — exec segment
    C_MEAN   = '#f0e080'   # yellow dashed — mean overhead line
    C_TEXT   = '#d0d0d0'
    C_GRID   = '#2e2e4e'
    C_SPINE  = '#44445e'

    root = tk.Tk()
    root.title('Tilted — Move Overhead')
    root.configure(bg=BG)

    fig = Figure(figsize=(11, 4), facecolor=BG)
    ax  = fig.add_subplot(111)

    canvas = FigureCanvasTkAgg(fig, master=root)
    canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

    after_id = [None]

    def _style_ax():
        ax.set_facecolor(BG)
        for sp in ax.spines.values():
            sp.set_edgecolor(C_SPINE)
        ax.tick_params(colors=C_TEXT)
        ax.xaxis.label.set_color(C_TEXT)
        ax.yaxis.label.set_color(C_TEXT)

    def on_close():
        if after_id[0] is not None:
            try:
                root.after_cancel(after_id[0])
            except Exception:
                pass
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)

    def update():
        try:
            ax.cla()
            _style_ax()

            history = []
            try:
                history = json.loads(data_file.read_text())
            except Exception:
                pass

            n = len(history)

            if n == 0:
                ax.text(
                    0.5, 0.5,
                    'No moves yet — waiting for engine moves...',
                    ha='center', va='center', transform=ax.transAxes,
                    color=C_TEXT, fontsize=12,
                )
                ax.set_title('Move Cycle Overhead', color=C_TEXT, fontsize=13)
            else:
                xs       = list(range(n))
                detect   = [h[0] for h in history]
                exec_t   = [h[2] for h in history]
                overhead = [h[3] for h in history]
                mean_oh  = sum(overhead) / n

                ax.bar(xs, detect, color=C_DETECT, label='detect', zorder=2)
                ax.bar(xs, exec_t, bottom=detect, color=C_EXEC,
                       label='exec', zorder=2)
                ax.axhline(
                    mean_oh, color=C_MEAN, linestyle='--', linewidth=1.3,
                    label=f'mean {mean_oh:.0f} ms', zorder=3,
                )
                ax.set_xlabel(
                    'Move  (oldest → newest)', color=C_TEXT, fontsize=9
                )
                ax.set_ylabel('Time (ms)', color=C_TEXT, fontsize=9)
                ax.set_title(
                    f'Move Cycle Overhead — last {n}  |  '
                    f'last={overhead[-1]} ms    avg={mean_oh:.0f} ms    '
                    f'max={max(overhead)} ms',
                    color=C_TEXT, fontsize=10,
                )
                ax.set_xlim(-0.6, max(n, 1) - 0.4)
                ax.set_ylim(0, max(max(overhead) * 1.30, 200))
                ax.set_xticks(xs)
                ax.set_xticklabels(
                    [str(i + 1) for i in range(n)],
                    color=C_TEXT, fontsize=7,
                )
                ax.legend(
                    loc='upper left',
                    facecolor='#2a2a3e', edgecolor=C_SPINE,
                    labelcolor=C_TEXT, fontsize=8,
                )
                ax.grid(axis='y', color=C_GRID, linewidth=0.7, zorder=0)

            fig.tight_layout()
            canvas.draw()
            after_id[0] = root.after(500, update)
        except tk.TclError:
            pass  # Window was closed — stop scheduling further redraws

    update()
    root.mainloop()


if __name__ == '__main__':
    main()
