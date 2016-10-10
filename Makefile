DOC=doc

all:    ${DOC}.pdf


gitrevision:
	git rev-parse HEAD > gitrevision


${DOC}.pdf: gitrevision dataset.tex concepts.tex
	latexmk -pdf -pdflatex="pdflatex -interactive=nonstopmode -shell-escape" -use-make ${DOC}.tex

clean:
	latexmk -CA
	rm ${DOC}.pdf
